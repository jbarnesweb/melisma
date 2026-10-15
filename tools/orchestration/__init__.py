"""Local, internal tool adapters; no server or command-execution endpoint."""

from .adapters import FFmpegAdapter, MelismaAdapter, OpenUtauAdapter
from .capabilities import (
    Capability, DiscoveryReport, Provenance, Readiness, SourceIdentity, SourceKind,
    State, Verification, resolve_default,
)
from .discovery import DiscoveryService, DiscoverySource, SingerRoot
from .playback import FFplayAdapter, PlaybackState
from .process import ProcessRunner
from .yue2 import InstrumentalAdapter, NativeYuE2Adapter

__all__ = [
    "Capability",
    "DiscoveryReport",
    "DiscoveryService",
    "DiscoverySource",
    "FFmpegAdapter",
    "FFplayAdapter",
    "InstrumentalAdapter",
    "MelismaAdapter",
    "NativeYuE2Adapter",
    "OpenUtauAdapter",
    "PlaybackState",
    "ProcessRunner",
    "Provenance",
    "Readiness",
    "SingerRoot",
    "SourceIdentity",
    "SourceKind",
    "State",
    "Verification",
    "resolve_default",
]
