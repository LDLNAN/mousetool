//! Single-threaded native HID executor. The GUI keeps one job in flight and
//! coalesces selection changes while waiting for its reply.

use iced::futures::channel::oneshot;
use mousetool_core::drivers::{
    LodState, MouseStatus, MouseUpdates, apply_mouse_updates, read_mouse_status,
};
use mousetool_core::hid::{DetectedMouse, discover_mice};
use mousetool_core::registry::DeviceRegistry;
use std::sync::mpsc::{SyncSender, sync_channel};

/// The registry ID alone can be shared by serial-less units of the same model.
#[derive(Clone, PartialEq, Eq)]
pub struct DeviceKey(String, String);

impl DeviceKey {
    pub fn of(mouse: &DetectedMouse) -> Self {
        Self(mouse.id.clone(), mouse.path.clone())
    }
}

#[derive(Clone, PartialEq, Eq)]
pub struct Tag {
    pub device: Option<DeviceKey>,
    pub generation: u64,
    pub sequence: u64,
}

pub enum Job {
    Discover,
    Read(DetectedMouse),
    Write(DetectedMouse, MouseUpdates),
}

#[derive(Clone)]
pub enum Outcome {
    Fleet(Vec<DetectedMouse>),
    DiscoveryFailed,
    Disconnected,
    Readback {
        status: MouseStatus,
        write_verified: Option<bool>,
    },
    Stale {
        after_write: bool,
    },
    WorkerUnavailable,
}

#[derive(Clone)]
pub struct Reply {
    pub tag: Tag,
    pub outcome: Outcome,
}

// iced messages can be debug-logged. Keep HID paths, IDs and status out of logs.
impl std::fmt::Debug for Reply {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("DeviceReply")
            .field("generation", &self.tag.generation)
            .field("sequence", &self.tag.sequence)
            .finish_non_exhaustive()
    }
}

type Request = (Tag, Job, oneshot::Sender<Reply>);

pub struct Worker {
    queue: SyncSender<Request>,
    #[cfg(test)]
    pub replies: std::sync::mpsc::Receiver<Reply>,
}

impl Worker {
    pub fn native(registry: std::sync::Arc<DeviceRegistry>) -> Self {
        Self::start(move |job| run_native(&registry, job))
    }

    pub fn start(mut run: impl FnMut(Job) -> Outcome + Send + 'static) -> Self {
        let (queue, requests) = sync_channel::<Request>(1);
        #[cfg(test)]
        let (observe, replies) = std::sync::mpsc::channel();
        // If spawning fails, dropping the closure closes the queue. The caller
        // receives WorkerUnavailable through the same completion path.
        let _ = std::thread::Builder::new()
            .name("device-io".into())
            .spawn(move || {
                while let Ok((tag, job, reply)) = requests.recv() {
                    let outcome = run(job);
                    let result = Reply { tag, outcome };
                    #[cfg(test)]
                    let _ = observe.send(result.clone());
                    let _ = reply.send(result);
                }
            });
        Self {
            queue,
            #[cfg(test)]
            replies,
        }
    }

    pub fn submit(&self, tag: Tag, job: Job) -> impl Future<Output = Reply> + use<> {
        let (send, receive) = oneshot::channel();
        let _ = self.queue.try_send((tag.clone(), job, send));
        async move {
            receive.await.unwrap_or(Reply {
                tag,
                outcome: Outcome::WorkerUnavailable,
            })
        }
    }
}

fn run_native(registry: &DeviceRegistry, job: Job) -> Outcome {
    let (mouse, updates) = match job {
        Job::Discover => {
            return match discover_mice(registry) {
                Ok(mice) => Outcome::Fleet(mice),
                Err(_) => Outcome::DiscoveryFailed,
            };
        }
        Job::Read(mouse) => (mouse, None),
        Job::Write(mouse, updates) => (mouse, Some(updates)),
    };

    let present = |mice: Vec<DetectedMouse>| {
        let key = DeviceKey::of(&mouse);
        mice.iter().any(|candidate| DeviceKey::of(candidate) == key)
    };
    if updates.is_some() {
        match discover_mice(registry).map(&present) {
            Ok(true) => {}
            Ok(false) => return Outcome::Disconnected,
            Err(_) => return Outcome::DiscoveryFailed,
        }
    }
    let result = match &updates {
        Some(updates) => apply_mouse_updates(&mouse, updates),
        None => read_mouse_status(&mouse),
    };
    // Check presence again before publishing even a successful read. A write
    // error can represent a partial write or a failed readback after a write.
    match discover_mice(registry).map(present) {
        Ok(true) => {}
        Ok(false) => return Outcome::Disconnected,
        Err(_) if updates.is_none() => return Outcome::DiscoveryFailed,
        Err(_) => return Outcome::Stale { after_write: true },
    }
    assess_readback(&mouse, updates.as_ref(), result)
}

pub(crate) fn assess_readback(
    mouse: &DetectedMouse,
    updates: Option<&MouseUpdates>,
    result: Result<MouseStatus, String>,
) -> Outcome {
    let Ok(mut status) = result else {
        return Outcome::Stale {
            after_write: updates.is_some(),
        };
    };
    status.serial = None;
    if !complete_settings(mouse, &status) {
        return Outcome::Stale {
            after_write: updates.is_some(),
        };
    }
    Outcome::Readback {
        write_verified: updates.map(|updates| matches_update(updates, &status)),
        status,
    }
}

// Razer can return a partial snapshot even when individual reports failed.
fn complete_settings(mouse: &DetectedMouse, status: &MouseStatus) -> bool {
    let device = &mouse.classified;
    device.driver != "razer"
        || ((!device.can_read("dpi") || status.dpi.is_some())
            && (!device.can_read("stages") || status.active_stage.is_some())
            && (!device.can_read("polling") || status.polling_hz.is_some())
            && (!device.can_read("lod") || status.lod.is_some()))
}

fn matches_update(update: &MouseUpdates, status: &MouseStatus) -> bool {
    macro_rules! matches_field {
        ($field:ident) => {
            update
                .$field
                .as_ref()
                .is_none_or(|value| status.$field.as_ref() == Some(value))
        };
    }
    update
        .stages
        .as_ref()
        .is_none_or(|stages| stages == &status.stages)
        && matches_field!(active_stage)
        && matches_field!(polling_hz)
        && matches_field!(motion_sync)
        && matches_field!(angle_snapping)
        && matches_field!(ripple_control)
        && matches_field!(slamclick)
        && matches_field!(motion_jitter)
        && matches_field!(left_handed)
        && matches_field!(spdt)
        && update.lod_index.is_none_or(
            |wanted| matches!(status.lod, Some(LodState::Index { index, .. }) if index == wanted),
        )
        && update.lod_preset.as_ref().is_none_or(
            |wanted| matches!(&status.lod, Some(LodState::Preset(actual)) if actual == wanted),
        )
}
