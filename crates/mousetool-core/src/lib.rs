//! Shared core library for Mouse Tool (portable app, installed app, and background helper).

pub mod accel;
pub mod hid;
pub mod registry;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RuntimeMode {
    /// Self-contained portable mode: executes directly in-process with zero installation
    /// and zero config writes to disk.
    PortableInProcess,
    /// Installed mode: communicates with the tiny `mousetool-agent` daemon over local IPC.
    AgentIpc,
}

impl RuntimeMode {
    pub fn badge_label(self) -> &'static str {
        match self {
            Self::PortableInProcess => "Portable · In-process",
            Self::AgentIpc => "Helper · IPC",
        }
    }
}
