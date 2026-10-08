"""Use the installed SO101 drivers on the app's already-owned serial bus.

No connect/configure/disconnect call is made here: those are attended lifecycle
operations. Driver reads and writes, normalization and default settings are used
unchanged. The cache exposes the raw observation from the SAME serial read.
"""
from types import SimpleNamespace


class ObservationBus:
    def __init__(self, bus):
        self.device = bus
        self.raw = None

    def __getattr__(self, name):
        return getattr(self.device, name)

    def sync_read(self, name, *args, **kwargs):
        if name != "Present_Position":
            return self.device.sync_read(name, *args, **kwargs)
        self.raw = self.device.sync_read(name, *args, **{**kwargs, "normalize": False})
        normalized = self.device._normalize({self.device.motors[n].id: v for n, v in self.raw.items()})
        return {n: normalized[self.device.motors[n].id] for n in self.raw}


class NativeTeleop:
    def __init__(self, device):
        from lerobot.robots.so_follower import SO101Follower, SO101FollowerConfig
        from lerobot.teleoperators.so_leader import SO101Leader, SO101LeaderConfig
        self.bus = ObservationBus(device.bus)
        self.role = device.role
        self.config = (SO101LeaderConfig if self.role == "leader" else SO101FollowerConfig)(port=device.bus.port)
        # These methods need only a connected bus, config and cameras. This view
        # avoids constructing another robot, opening another port, or loading a
        # second calibration. The app's verified bus remains the sole owner.
        self.view = SimpleNamespace(bus=self.bus, config=self.config, cameras={}, is_connected=True)
        self.read_method = SO101Leader.get_action if self.role == "leader" else SO101Follower.get_observation
        self.send_method = SO101Follower.send_action

    @property
    def settings(self):
        return {"driver": "LeRobot SO101", "use_degrees": self.config.use_degrees,
                "num_read_retries": self.config.num_read_retries,
                "max_relative_target": getattr(self.config, "max_relative_target", None)}

    def read(self):
        self.view.is_connected = self.bus.is_connected
        action = self.read_method(self.view)
        return dict(self.bus.raw), {n.removesuffix('.pos'): v for n, v in action.items() if n.endswith('.pos')}

    def send(self, action):
        if self.role != "follower":
            raise RuntimeError("The leader is input-only")
        self.view.is_connected = self.bus.is_connected
        result = self.send_method(self.view, {f"{n}.pos": v for n, v in action.items()})
        return {n.removesuffix('.pos'): v for n, v in result.items()}
