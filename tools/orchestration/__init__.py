"""Local, internal tool adapters; no server or command-execution endpoint."""

from .adapters import FFmpegAdapter, MelismaAdapter, OpenUtauAdapter
from .capabilities import (
    Capability, DiscoveryReport, Provenance, Readiness, SourceIdentity, SourceKind,
    State, Verification, resolve_default, revalidate_selection,
)
from .configuration import (
    AsyncAPI, AuthReference, Configuration, ConfigurationError, ConfigurationIssue,
    ExecutionContract, GenerationDefaults, LoadedConfiguration, MixSettings,
    ModelIdentity, Preset, PresetValidation, SSHMetadata, SamplingDefaults,
    SavedSelection, UnsupportedVersion, YuE2Target, gloria_preset,
    load_configuration, load_preset, save_configuration, save_preset,
    validate_configuration,
)
from .discovery import DiscoveryService, DiscoverySource, SingerRoot
from .playback import FFplayAdapter, PlaybackState
from .process import ProcessRunner
from .yue2 import InstrumentalAdapter, NativeYuE2Adapter

__all__ = [
    "AsyncAPI",
    "AuthReference",
    "Capability",
    "Configuration",
    "ConfigurationError",
    "ConfigurationIssue",
    "DiscoveryReport",
    "DiscoveryService",
    "DiscoverySource",
    "FFmpegAdapter",
    "FFplayAdapter",
    "ExecutionContract",
    "GenerationDefaults",
    "InstrumentalAdapter",
    "MelismaAdapter",
    "LoadedConfiguration",
    "MixSettings",
    "ModelIdentity",
    "NativeYuE2Adapter",
    "OpenUtauAdapter",
    "PlaybackState",
    "ProcessRunner",
    "Provenance",
    "Preset",
    "PresetValidation",
    "Readiness",
    "SingerRoot",
    "SSHMetadata",
    "SamplingDefaults",
    "SavedSelection",
    "SourceIdentity",
    "SourceKind",
    "State",
    "Verification",
    "UnsupportedVersion",
    "YuE2Target",
    "gloria_preset",
    "load_configuration",
    "load_preset",
    "save_configuration",
    "save_preset",
    "validate_configuration",
    "revalidate_selection",
    "resolve_default",
]
