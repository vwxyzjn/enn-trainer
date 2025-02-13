# adapted from https://github.com/vwxyzjn/cleanrl
import inspect
import json
import os
import random
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Type, Union

import click
import hyperstate
import numpy as np
import torch
import torch.distributed as dist
import torch.nn as nn
import torch.optim as optim
from entity_gym.env import *
from entity_gym.env.add_metrics_wrapper import AddMetricsWrapper
from entity_gym.env.validator import ValidatingEnv
from entity_gym.env.vec_env import Metric
from entity_gym.examples import ENV_REGISTRY
from entity_gym.serialization import SampleRecordingVecEnv
from entity_gym.simple_trace import Tracer
from hyperstate import StateManager
from rogue_net.rogue_net import RogueNet
from torch.utils.tensorboard import SummaryWriter

from enn_trainer.agent import PPOAgent
from enn_trainer.config import *
from enn_trainer.eval import allgather, run_eval
from enn_trainer.gae import returns_and_advantages
from enn_trainer.ppo import ppo_loss, value_loss
from enn_trainer.rollout import Rollout

EnvFactory = Callable[[EnvConfig, int, int, int], VecEnv]


class SerializableRogueNet(RogueNet, hyperstate.Serializable[TrainConfig, "State"]):
    def serialize(self) -> Any:
        return self.state_dict()

    @classmethod
    def deserialize(
        clz, state_dict: Any, config: TrainConfig, state: "State", ctx: Dict[str, Any]
    ) -> "SerializableRogueNet":
        net = SerializableRogueNet(
            config.net,
            state.obs_space,
            state.action_space,
            regression_heads={"value": 1},
        )
        net.load_state_dict(state_dict)
        return net


class SerializableAdamW(optim.AdamW, hyperstate.Serializable):
    def serialize(self) -> Any:
        return self.state_dict()

    @classmethod
    def deserialize(
        clz, state_dict: Any, config: TrainConfig, state: "State", ctx: Dict[str, Any]
    ) -> "SerializableAdamW":
        optimizer = SerializableAdamW(
            state.agent.parameters(),
            lr=config.optim.lr,
            weight_decay=config.optim.weight_decay,
            eps=1e-5,
        )
        optimizer.load_state_dict(state_dict)
        return optimizer


def _env_factory(
    env_cls: Type[Environment],
) -> Callable[[EnvConfig, int, int, int], VecEnv]:
    def _create_env(
        cfg: EnvConfig, num_envs: int, processes: int, first_env_index: int
    ) -> VecEnv:
        kwargs = json.loads(cfg.kwargs)
        if cfg.validate:
            create_env = lambda: ValidatingEnv(env_cls(**kwargs))
        else:
            create_env = lambda: env_cls(**kwargs)  # type: ignore
        if processes > 1:
            return ParallelEnvList(create_env, num_envs, processes)
        else:
            return EnvList(create_env, num_envs)

    return _create_env


def load_rogue_net_opponent(
    path: str,
    obs_space: ObsSpace,
    action_space: Mapping[str, ActionSpace],
    device: torch.device,
) -> PPOAgent:
    if path == "random":
        return RogueNet(
            RogueNetConfig(),
            obs_space,
            dict(action_space),
            regression_heads={"value": 1},
        ).to(device)
    else:
        return StateManager(
            TrainConfig,
            State,
            init_train_state,
            init_path=path,
            ignore_extra_fields=True,
        ).state.agent.to(device)


@dataclass
class State(hyperstate.Lazy):
    """
    Mutable state of training run.

    :param step: The number of elapsed environment steps.
    :param restart: The number of times the training has been restarted from a checkpoint.
    :param agent: The policy network.
    :param value_function: The value function, if separate from the policy network.
    :param optimizer: AdamW optimizer for the policy network.
    :param value_optimizer: AdamW optimizer for the value function, if separate from the policy network.
    :param obs_space: The observation space of the environment.
    :param action_space: The action space of the environment.
    """

    step: int
    restart: int
    next_eval_step: Optional[int]
    agent: SerializableRogueNet
    value_function: Optional[SerializableRogueNet]
    optimizer: SerializableAdamW
    vf_optimizer: Optional[SerializableAdamW]
    obs_space: ObsSpace
    action_space: Dict[str, ActionSpace]


def train(
    state_manager: StateManager[TrainConfig, State],
    env: Union[Type[Environment], EnvFactory],
    create_opponent: Optional[
        Callable[[str, ObsSpace, Mapping[str, ActionSpace], torch.device], PPOAgent]
    ] = None,
    agent: Optional[PPOAgent] = None,
) -> float:
    """
    Train an agent in an entity-gym environment using proximal policy optimization.

    :param state_manager: The hyperstate StateManager encapsulates the configuration and mutable state of
        the training run.
    :param env: The class of the entity-gym environment to train on.
    :param create_opponent: A function that creates a new opponent agent used for evaluating the agent.
    :param agent: Custom policy network to use.
    """
    if state_manager.checkpoint_dir is None and os.path.exists(
        "/xprun/info/config.ron"
    ):
        import xprun  # type: ignore

        xp_info = xprun.current_xp()
        if xp_info.replica_index == 0:
            state_manager.checkpoint_dir = (
                Path("/xprun/data")
                / xp_info.xp_def.project
                / (xp_info.sanitized_name + "-" + xp_info.id)
                / "checkpoints"
            )

    cfg = state_manager.config
    cuda = torch.cuda.is_available() and cfg.cuda
    device = torch.device("cuda" if cuda else "cpu")

    assert cfg.rollout.num_envs * cfg.rollout.steps >= cfg.optim.bs, (
        "Number of frames per rollout is smaller than batch size: "
        f"{cfg.rollout.num_envs} * {cfg.rollout.steps} < {cfg.optim.bs}"
    )

    run_name = f"{cfg.env.id}__{cfg.name}__{cfg.seed}__{int(time.time())}"

    config = asdict(cfg)
    if os.path.exists("/xprun/info/config.ron"):
        import xprun

        xp_info = xprun.current_xp()
        config["name"] = xp_info.xp_def.name
        config["base_name"] = xp_info.xp_def.base_name
        config["id"] = xp_info.id
        if "-" in xp_info.xp_def.name and xp_info.xp_def.name.split("-")[-1].isdigit():
            cfg.seed = int(xp_info.xp_def.name.split("-")[-1])
            config["seed"] = cfg.seed
        run_name = xp_info.xp_def.name
        out_dir: Optional[str] = os.path.join(
            "/xprun/data",
            xp_info.xp_def.project,
            xp_info.sanitized_name + "-" + xp_info.id,
        )
        id = xp_info.id
        Path(str(out_dir)).mkdir(parents=True, exist_ok=True)

        init_process(xp_info)
        rank = xp_info.replica_index
        parallelism = xp_info.replicas()
    else:
        id = None
        out_dir = None
        rank = 0
        parallelism = 1

    assert cfg.optim.bs % parallelism == 0, (
        "Batch size must be divisible by number of processes: "
        f"{cfg.optim.bs} % {parallelism} != 0"
    )
    assert cfg.rollout.num_envs % parallelism == 0, (
        "Number of environments must be divisible by number of processes: "
        f"{cfg.rollout.num_envs} % {parallelism} != 0"
    )

    data_path = Path(cfg.data_dir).absolute()
    data_path.mkdir(parents=True, exist_ok=True)
    data_dir = str(data_path)

    random.seed(cfg.seed)
    np.random.seed(cfg.seed)
    torch.manual_seed(cfg.seed)
    torch.backends.cudnn.deterministic = cfg.torch_deterministic

    if inspect.isclass(env) and issubclass(env, Environment):
        create_env: EnvFactory = _env_factory(env)
    else:
        create_env = env  # type: ignore
    envs: VecEnv = AddMetricsWrapper(
        create_env(
            cfg.env,
            cfg.rollout.num_envs // parallelism,
            cfg.rollout.processes,
            rank * cfg.rollout.num_envs // parallelism,
        ),
    )
    obs_space = envs.obs_space()
    action_space = envs.action_space()

    state_manager.set_deserialize_ctx("obs_space", obs_space)
    state_manager.set_deserialize_ctx("action_space", action_space)
    state_manager.set_deserialize_ctx("agent", agent)
    state = state_manager.state
    if rank != 0:
        state_manager.checkpoint_dir = None
    if state.step > 0:
        state.restart += 1
    agent = state.agent.to(device)
    optimizer = state.optimizer
    value_function = state.value_function
    if value_function is not None:
        value_function = value_function.to(device)
    vf_optimizer = state.vf_optimizer

    tracer = Tracer(cuda=cuda)

    if cfg.capture_samples and rank == 0:
        if out_dir is None:
            sample_file = cfg.capture_samples
        else:
            sample_file = os.path.join(out_dir, cfg.capture_samples)
        envs = SampleRecordingVecEnv(envs, sample_file, cfg.capture_samples_subsample)

    rollout = Rollout(
        envs,
        obs_space=obs_space,
        action_space=action_space,
        agent=agent,
        value_function=value_function,
        device=device,
        tracer=tracer,
    )

    if rank == 0:
        if cfg.track:
            import wandb

            wandb.init(
                project=cfg.wandb_project_name,
                entity=cfg.wandb_entity,
                sync_tensorboard=True,
                config=config,
                name=run_name,
                save_code=True,
                dir=data_dir,
                id=id,
            )
            wandb.watch(agent)

        writer = SummaryWriter(os.path.join(data_dir, f"runs/{run_name}"))

        def flatten(config: Dict[str, Any], prefix: str = "") -> Dict[str, Any]:
            flattened = {}
            for k, v in config.items():
                if isinstance(v, dict):
                    flattened.update(flatten(v, k if prefix == "" else f"{prefix}.{k}"))
                else:
                    flattened[prefix + k] = v
            return flattened

        writer.add_text(
            "hyperparameters",
            "|param|value|\n|-|-|\n%s"
            % (
                "\n".join(
                    [f"|{key}|{value}|" for key, value in flatten(config).items()]
                )
            ),
        )

    def _run_eval() -> None:
        if cfg.eval is not None:
            assert create_env is not None
            assert agent is not None
            with tracer.span("eval"):
                run_eval(
                    cfg.eval,
                    cfg.env,
                    cfg.rollout,
                    create_env,
                    create_opponent or load_rogue_net_opponent,
                    agent,
                    device,
                    tracer,
                    writer if rank == 0 else None,
                    rollout.global_step * parallelism,
                    rank,
                    parallelism,
                )

    start_time = time.time()
    num_updates = cfg.total_timesteps // (cfg.rollout.num_envs * cfg.rollout.steps)
    initial_step = state.step
    for update in range(
        1 + initial_step // (cfg.rollout.num_envs * cfg.rollout.steps), num_updates + 1
    ):
        if (
            cfg.eval is not None
            and state.next_eval_step is not None
            and rollout.global_step * parallelism >= state.next_eval_step
        ):
            state.next_eval_step += cfg.eval.interval
            _run_eval()

        tracer.start("update")
        if (
            cfg.max_train_time is not None
            and time.time() - start_time >= cfg.max_train_time
        ):
            print("Max train time reached, stopping training.")
            break

        # Annealing the rate if instructed to do so.
        if cfg.optim.anneal_lr:
            frac = 1.0 - (update - 1.0) / num_updates
            if cfg.max_train_time is not None:
                frac = min(
                    frac, max(0, 1.0 - (time.time() - start_time) / cfg.max_train_time)
                )
            lrnow = frac * cfg.optim.lr
            optimizer.param_groups[0]["lr"] = lrnow
            if vf_optimizer is not None:
                vf_optimizer.param_groups[0]["lr"] = lrnow

        tracer.start("rollout")

        next_obs, next_done, metrics = rollout.run(
            cfg.rollout.steps, record_samples=True, capture_logits=cfg.capture_logits
        )

        global_step = rollout.global_step * parallelism + initial_step

        if parallelism > 1:
            serialized_metrics = json.dumps(
                {
                    k: {
                        "count": int(v.count),
                        "sum": float(v.sum),
                        "min": float(v.min),
                        "max": float(v.max),
                    }
                    for k, v in metrics.items()
                }
            )
            metrics_tensor = torch.tensor(
                bytearray(serialized_metrics.encode("utf-8")), dtype=torch.uint8
            )
            metrics = {}
            for metrics_tensor in allgather(metrics_tensor, rank, parallelism):
                for k, v in json.loads(
                    metrics_tensor.numpy().tobytes().decode("utf-8")
                ).items():
                    m = Metric(**v)
                    if k in metrics:
                        metrics[k] += m
                    else:
                        metrics[k] = m
        if rank == 0:
            for name, value in metrics.items():
                writer.add_scalar(f"{name}.mean", value.mean, global_step)
                writer.add_scalar(f"{name}.max", value.max, global_step)
                writer.add_scalar(f"{name}.min", value.min, global_step)
                writer.add_scalar(f"{name}.count", value.count, global_step)

        values = rollout.values
        actions = rollout.actions
        entities = rollout.entities
        visible = rollout.visible
        action_masks = rollout.action_masks
        logprobs = rollout.logprobs

        with torch.no_grad(), tracer.span("advantages"):
            returns, advantages = returns_and_advantages(
                value_function or agent,
                next_obs,
                next_done,
                rollout.rewards,
                rollout.dones,
                values,
                cfg.ppo.gae,
                cfg.ppo.gamma,
                cfg.ppo.gae_lambda,
                device,
                tracer,
            )

        # flatten the batch
        with tracer.span("flatten"):
            b_advantages = advantages.reshape(-1)
            b_returns = returns.reshape(-1).detach()
            b_values = values.reshape(-1).detach()

        tracer.end("rollout")

        # Optimize the policy and value network
        tracer.start("optimize")
        frames = cfg.rollout.num_envs * cfg.rollout.steps // parallelism
        b_inds = np.arange(frames)
        clipfracs = []

        for epoch in range(cfg.optim.update_epochs):
            np.random.shuffle(b_inds)
            for start in range(0, frames, cfg.optim.bs // parallelism):
                end = start + cfg.optim.bs // parallelism
                microbatch_size = (
                    cfg.optim.micro_bs
                    if cfg.optim.micro_bs is not None
                    else cfg.optim.bs // parallelism
                )

                optimizer.zero_grad()
                if vf_optimizer is not None:
                    vf_optimizer.zero_grad()
                for _start in range(start, end, microbatch_size):
                    _end = _start + microbatch_size
                    mb_inds = b_inds[_start:_end]

                    b_entities = entities[mb_inds]
                    b_visible = visible[mb_inds]
                    b_action_masks = action_masks[mb_inds]
                    b_logprobs = logprobs[mb_inds]
                    b_actions = actions[mb_inds]
                    mb_advantages = b_advantages[mb_inds]  # type: ignore

                    with tracer.span("forward"):
                        (
                            _,
                            newlogprob,
                            entropy,
                            _,
                            aux,
                            _,
                        ) = agent.get_action_and_auxiliary(
                            b_entities,
                            b_visible,
                            b_action_masks,
                            prev_actions=b_actions,
                            tracer=tracer,
                        )
                        if value_function is None:
                            newvalue = aux["value"]
                        else:
                            newvalue = value_function.get_auxiliary_head(
                                b_entities, b_visible, "value", tracer=tracer
                            )

                    pg_loss, clipfrac, approx_kl = ppo_loss(
                        cfg.ppo, newlogprob, b_logprobs, mb_advantages, device, tracer
                    )
                    clipfracs += [clipfrac]

                    v_loss = value_loss(
                        cfg.ppo,
                        newvalue,
                        b_returns[mb_inds],  # type: ignore
                        b_values[mb_inds],  # type: ignore
                        tracer,
                    )

                    # TODO: what's correct way of combining entropy loss from multiple actions/actors on the same timestep?
                    if cfg.ppo.anneal_entropy:
                        frac = 1.0 - (update - 1.0) / num_updates
                        if cfg.max_train_time is not None:
                            frac = min(
                                frac,
                                max(
                                    0,
                                    1.0
                                    - (time.time() - start_time) / cfg.max_train_time,
                                ),
                            )
                        ent_coef = frac * cfg.ppo.ent_coef
                    else:
                        ent_coef = cfg.ppo.ent_coef
                    entropy_loss = torch.cat([e for e in entropy.values()]).mean()
                    loss = pg_loss - ent_coef * entropy_loss + v_loss * cfg.ppo.vf_coef
                    loss *= microbatch_size / cfg.optim.bs

                    with tracer.span("backward"):
                        loss.backward()
                if parallelism > 1:
                    with tracer.span("allreduce"):
                        gradient_allreduce(agent)
                gradnorm = nn.utils.clip_grad_norm_(
                    agent.parameters(), cfg.optim.max_grad_norm
                )
                optimizer.step()
                if value_function is not None:
                    if parallelism > 1:
                        with tracer.span("allreduce_vf"):
                            gradient_allreduce(value_function)
                    vf_gradnorm = nn.utils.clip_grad_norm_(
                        value_function.parameters(), cfg.optim.max_grad_norm
                    ).item()
                else:
                    vf_gradnorm = 0.0
                if vf_optimizer is not None:
                    vf_optimizer.step()

            if cfg.ppo.target_kl is not None:
                if approx_kl > cfg.ppo.target_kl:
                    break

        if cfg.cuda_empty_cache:
            torch.cuda.empty_cache()
        tracer.end("optimize")

        tracer.start("metrics")
        # TODO: aggregate across all ranks
        y_pred, y_true = b_values.cpu().numpy(), b_returns.cpu().numpy()
        var_y = np.var(y_true)
        explained_var = torch.tensor(
            np.nan if var_y == 0 else 1 - np.var(y_true - y_pred) / var_y
        )
        clipfrac = torch.tensor(np.mean(clipfracs))
        if parallelism > 1:
            dist.all_reduce(v_loss, op=dist.ReduceOp.SUM)
            dist.all_reduce(pg_loss, op=dist.ReduceOp.SUM)
            dist.all_reduce(entropy_loss, op=dist.ReduceOp.SUM)
            dist.all_reduce(approx_kl, op=dist.ReduceOp.SUM)
            dist.all_reduce(clipfrac, op=dist.ReduceOp.SUM)
            dist.all_reduce(explained_var, op=dist.ReduceOp.SUM)
            v_loss /= parallelism
            pg_loss /= parallelism
            entropy_loss /= parallelism
            approx_kl /= parallelism
            clipfrac /= parallelism
            explained_var /= parallelism
        if rank == 0:

            writer.add_scalar(
                "charts/learning_rate", optimizer.param_groups[0]["lr"], global_step
            )
            writer.add_scalar("charts/entropy_coef", ent_coef, global_step)
            writer.add_scalar("losses/value_loss", v_loss.item(), global_step)
            writer.add_scalar("losses/policy_loss", pg_loss.item(), global_step)
            writer.add_scalar("losses/entropy", entropy_loss.item(), global_step)
            writer.add_scalar("losses/approx_kl", approx_kl.item(), global_step)
            writer.add_scalar("losses/clipfrac", clipfrac.item(), global_step)
            writer.add_scalar(
                "losses/explained_variance", explained_var.item(), global_step
            )
            writer.add_scalar("losses/gradnorm", gradnorm, global_step)
            writer.add_scalar("losses/vf_gradnorm", vf_gradnorm, global_step)
            writer.add_scalar("restart", state.restart, global_step)
            # TODO: aggregate actions across ranks
            for action_name, space in action_space.items():
                if isinstance(space, CategoricalActionSpace):
                    _actions = actions.buffers[action_name].as_array().flatten()
                    if len(_actions) > 0:
                        for i, label in enumerate(space.index_to_label):
                            writer.add_scalar(
                                f"actions/{action_name}/{label}",
                                np.sum(_actions == i).item() / len(_actions),
                                global_step,
                            )

            fps = (global_step - initial_step) / (time.time() - start_time)
            digits = int(np.ceil(np.log10(cfg.total_timesteps)))
            episodic_reward = metrics["episodic_reward"].mean
            episode_length = metrics["episode_length"].mean
            episode_count = metrics["episode_length"].count
            mean_reward = metrics["reward"].mean

            def green(s: str) -> str:
                return click.style(s, fg="cyan")

            def estyle(f: float) -> str:
                return click.style(f"{f:.2e}", fg="cyan")

            def fstyle(f: float) -> str:
                return click.style(f"{f:5.2f}", fg="cyan")

            def tstyle(s: str) -> str:
                return s

            def symstyle(s: str) -> str:
                return click.style(s, fg="white", bold=True)

            # fmt: off
            click.echo(
                green(f"{global_step:>{digits}}") + symstyle("/") + green(f"{cfg.total_timesteps} ")
                + f"{symstyle('|')} {tstyle('meanrew')} {estyle(mean_reward)} "
                + f"{symstyle('|')} {tstyle('explained_var')} {fstyle(explained_var.item())} "
                + f"{symstyle('|')} {tstyle('entropy')} {fstyle(entropy_loss.item())} "
                + f"{symstyle('|')} {tstyle('episodic_reward')} {estyle(episodic_reward)} "
                + f"{symstyle('|')} {tstyle('episode_length')} {estyle(episode_length)} "
                + f"{symstyle('|')} {tstyle('episodes')} {green(str(episode_count))} "
                + f"{symstyle('|')} {tstyle('fps')} {green(str(int(fps)))}"
            )
            # fmt: on
            writer.add_scalar(
                "charts/SPS",
                int((global_step - initial_step) / (time.time() - start_time)),
                global_step,
            )
        tracer.end("metrics")
        tracer.end("update")
        traces = tracer.finish()
        if rank == 0:
            for callstack, timing in traces.items():
                writer.add_scalar(f"trace/{callstack}", timing, global_step)

        state.step = global_step
        with tracer.span("checkpoint"):
            state_manager.step()

    if cfg.eval is not None:
        _run_eval()

    envs.close()
    if rank == 0:
        writer.close()

    return rollout.rewards.mean().item()


def init_process(xp_info: Any, backend: str = "gloo") -> None:
    os.environ["MASTER_ADDR"] = xp_info.address_of("main")
    os.environ["MASTER_PORT"] = "29500"
    dist.init_process_group(
        backend,
        rank=xp_info.replica_index,
        world_size=xp_info.replicas(),
    )


def gradient_allreduce(model: Any) -> None:
    all_grads_list = []
    for param in model.parameters():
        if param.grad is not None:
            all_grads_list.append(param.grad.view(-1))
    all_grads = torch.cat(all_grads_list)
    dist.all_reduce(all_grads, op=dist.ReduceOp.SUM)
    offset = 0
    for param in model.parameters():
        if param.grad is not None:
            param.grad.data.copy_(
                all_grads[offset : offset + param.numel()].view_as(param.grad.data)
            )
            offset += param.numel()


def _create_agent(
    cfg: TrainConfig, obs_space: ObsSpace, action_space: Dict[ActionName, ActionSpace]
) -> SerializableRogueNet:
    return SerializableRogueNet(
        cfg.net,
        obs_space,
        action_space,
        regression_heads={"value": 1},
    )


def init_train_state(cfg: TrainConfig, ctx: Dict[str, Any]) -> State:
    """
    Creates the initial state for training, given a config and context.
    """
    if cfg.eval is not None:
        if cfg.eval.run_on_first_step:
            next_eval_step: Optional[int] = 0
        else:
            next_eval_step = cfg.eval.interval
    else:
        next_eval_step = None

    if ctx.get("agent") is not None:
        agent: SerializableRogueNet = ctx["agent"]
    else:
        agent = _create_agent(cfg, ctx["obs_space"], ctx["action_space"])
    optimizer = SerializableAdamW(
        agent.parameters(),
        lr=cfg.optim.lr,
        weight_decay=cfg.optim.weight_decay,
        eps=1e-5,
    )

    if cfg.vf_net is not None:
        value_function: Optional[SerializableRogueNet] = _create_agent(
            cfg, ctx["obs_space"], ctx["action_space"]
        )
        vf_optimizer: Optional[SerializableAdamW] = SerializableAdamW(
            value_function.parameters(),  # type: ignore
            lr=cfg.optim.lr,
            weight_decay=cfg.optim.weight_decay,
            eps=1e-5,
        )
    else:
        value_function = None
        vf_optimizer = None

    return State(
        step=0,
        restart=0,
        next_eval_step=next_eval_step,
        agent=agent,
        value_function=value_function,
        optimizer=optimizer,
        vf_optimizer=vf_optimizer,
        obs_space=ctx["obs_space"],
        action_space=ctx["action_space"],
    )


@hyperstate.stateful_command(TrainConfig, State, init_train_state)
def _main(state_manager: StateManager) -> None:
    env_cls = ENV_REGISTRY[state_manager.config.env.id]
    train(state_manager, env_cls)


if __name__ == "__main__":
    _main()
