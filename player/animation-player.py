# Python
from typing import Any
import argparse
import os
import yaml

# Isaac
from isaacsim import SimulationApp
import carb


def load_config(config_path: str, default_path: str) -> dict[str, Any]:
    if not config_path.endswith(".yaml"):
        carb.log_warn(f"File {config_path} is not yaml, will use default config")
        config_path = default_path

    with open(config_path, "r") as f:  # pylint: disable=unspecified-encoding
        return yaml.safe_load(f)  # type: ignore


def parse_config() -> dict[str, Any]:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-c",
        "--config",
        required=False,
        help="Include specific config parameters (yaml)",
        default=os.path.join(os.path.dirname(__file__), "config.yaml"),
    )

    args = parser.parse_args()
    args_dict = vars(args)
    config = load_config(args.config, default_path="config.yaml")
    config.update(args_dict)

    return config


def create_simulation_app(config: dict[str, Any]) -> SimulationApp:
    launch_config: dict[str, Any] = config["simulation_app"]["launch_config"]
    experience: str = config["simulation_app"]["experience"]
    return SimulationApp(launch_config, experience)


# Parse config
global_config = parse_config()

# Create simulation app
sim_app = create_simulation_app(global_config)

# Python
import time

# Isaac
from carb.events import IEvent  # pylint: disable=no-name-in-module
from omni.isaac.core import SimulationContext
from omni.isaac.core.utils.stage import get_current_stage
from omni.isaac.core.utils.stage import open_stage
from pxr.Sdf import Layer  # pylint: disable=no-name-in-module
import carb.events
import omni.kit.app
import omni.timeline
from omni.isaac.core.utils.extensions import enable_extension


class SteadyRate:
    """
    Maintains the steady cycle rate provided on initialization by adaptively sleeping an amount
    of time to make up the remaining cycle time after work is done.
    Usage:
    rate = SteadyRate(rate_hz=30.)
    while True:
      do.work()  # Do any work.
      rate.sleep()  # Sleep for the remaining cycle time.
    """

    def __init__(self, rate_hz: float) -> None:
        self.rate_hz = rate_hz
        self.dt = 1.0 / rate_hz
        self.last_sleep_end = time.time()

    def sleep(self) -> None:
        work_elapse = time.time() - self.last_sleep_end
        sleep_time = self.dt - work_elapse
        if sleep_time > 0.0:
            time.sleep(sleep_time)
        self.last_sleep_end = time.time()


class AnimationPlayer:
    """Application class to play the animation."""

    RATE_HZ = 60.0

    def __init__(self, simulation_app: SimulationApp) -> None:
        """
        Args:
            simulation_app (SimulationApp): SimulationApp instance.
        """
        self.simulation_app = simulation_app
        self._timeline = omni.timeline.get_timeline_interface()
        self._sim_context = SimulationContext(
            stage_units_in_meters=1.0,
            physics_dt=1.0 / self.RATE_HZ,
            rendering_dt=1.0 / self.RATE_HZ,
        )

        self._time_step_index = 0
        self._last_frame_index: int = global_config["last_frame_index"]
        enable_extension("omni.kit.livestream.webrtc")

    @property
    def last_frame_index(self) -> int:
        return self._last_frame_index

    @last_frame_index.setter
    def last_frame_index(self, value: int) -> None:
        self._last_frame_index = value

    def run_loop(self) -> None:
        """
        Run the animation. This method will run the animation until the application is running. Renders
        the world at a fixed rate. Loops the animation if the last frame is reached.
        """
        rate = SteadyRate(self.RATE_HZ)

        is_reset_pending = False
        self.play_from_start()

        while self.simulation_app.is_running():
            # Render world first to get input from user, e.g. Button for stopping animation
            self._sim_context.render()

            # Maintain rate
            rate.sleep()

            if self._timeline.is_stopped() and not is_reset_pending:
                is_reset_pending = True
            if self._timeline.is_playing() and is_reset_pending:
                self.play_from_start()
                is_reset_pending = False

            # If last frame is reached, stop the animation
            if self._time_step_index >= self._last_frame_index:
                self.play_from_start()

            if self._timeline.is_playing():
                self._time_step_index += 1

        # Cleanup
        self.simulation_app.close()

    def play_from_start(self) -> None:
        self._timeline.set_end_time(self.last_frame_index / 60)
        self._timeline.play(start_timecode=0, end_timecode=self._last_frame_index)
        self._time_step_index = 0


def initialize_animation(
    directory_path: str, last_frame_index: int, layer_paths: list[str] | None = None
) -> None:
    main_scene_path: str = directory_path + global_config["scene_name"]
    if not open_stage(main_scene_path):
        carb.log_error(
            f"Could not open stage [{main_scene_path}], closing application ..."
        )
        sim_app.close()
        return

    if layer_paths is not None:
        # By default insert index is 0, reverse the list to insert in the correct order (layers higher in the list have
        # higher priority and will overwrite layers below.)
        layer_paths.reverse()
        for layer_path in layer_paths:
            insert_layer(layer_path)

    app.last_frame_index = last_frame_index
    app.play_from_start()


app = AnimationPlayer(sim_app)


def main() -> None:
    load_usd_type = carb.events.type_from_string(
        "omni.sunrise.LOAD_USD"
    )  # pylint: disable=no-member

    # App provides common event bus. It is event queue which is popped every update (frame).
    bus = omni.kit.app.get_app().get_message_bus_event_stream()

    def on_event(e: IEvent) -> None:
        if e.type == load_usd_type:
            directory_path: str = e.payload.get("directory", None)
            last_frame_index: int = e.payload.get("last_frame_index", None)
            layer_paths_tuple = e.payload.get("layer_paths", None)
            animation_layer_path: list[str] | None = (
                list(layer_paths_tuple) if layer_paths_tuple is not None else None
            )

            initialize_animation(directory_path, last_frame_index, animation_layer_path)

    # Pop is called on next update
    _ = bus.create_subscription_to_pop_by_type(load_usd_type, on_event)

    if global_config.get("usd_directory", None) is not None:
        # And run the full animation
        initialize_animation(
            directory_path=global_config["usd_directory"],
            last_frame_index=global_config["last_frame_index"],
            layer_paths=global_config.get("layer_paths", None),
        )

    # And run the animation loop
    app.run_loop()


def insert_layer(layer_path: str, layer_index_position: int = 0) -> None:
    # Get the root layer
    root_layer: Layer = get_current_stage().GetRootLayer()

    # Create a new sub layer with the given path
    sub_layer: Layer | None = Layer.FindOrOpen(layer_path)

    if sub_layer is None:
        carb.log_error(f"Could not open layer [{layer_path}]")
        return

    # Insert the sub layer into to the first place in the subLayerPaths list
    root_layer.subLayerPaths.insert(layer_index_position, sub_layer.identifier)


if __name__ == "__main__":
    main()
