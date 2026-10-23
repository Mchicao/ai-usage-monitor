use windows::Win32::UI::Shell::{
    SHQueryUserNotificationState, QUNS_PRESENTATION_MODE, QUNS_RUNNING_D3D_FULL_SCREEN,
};

pub fn is_game_or_fullscreen_active() -> bool {
    unsafe {
        if let Ok(state) = SHQueryUserNotificationState() {
            // 3 = D3D fullscreen (BF6, DirectX), 4 = presentación/exclusivo
            state == QUNS_RUNNING_D3D_FULL_SCREEN || state == QUNS_PRESENTATION_MODE
        } else {
            false
        }
    }
}
