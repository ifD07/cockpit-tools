// Kept dependency-free so the shutdown guard can be regression-tested with
// rustc --test on the compatibility builder as well as through cargo test.
fn linux_pid_stat_is_live(stat: &str) -> bool {
    stat.rsplit_once(") ")
        .and_then(|(_, rest)| rest.chars().next())
        .map(|state| matches!(state, 'R' | 'S' | 'D' | 'T' | 't' | 'I' | 'P' | 'W'))
        .unwrap_or(false)
}

fn linux_codex_executable_matches(
    expected: &str, actual: &str, command_exe: Option<&str>,
) -> Result<bool, &'static str> {
    if actual == expected {
        Ok(true)
    } else if command_exe == Some(expected) || (actual.is_empty() && command_exe.is_none()) {
        Err("unverified Codex executable")
    } else {
        Ok(false)
    }
}

#[cfg(test)]
mod linux_shutdown_guard_tests {
    use super::*;

    #[test]
    fn zombies_and_dead_processes_do_not_extend_shutdown_wait() {
        for state in ["Z", "X", "x", "?"] {
            assert!(!linux_pid_stat_is_live(&format!("12 (ChatGPT) {} 1 2", state)));
        }
        for state in ["R", "S", "D", "T", "t", "I"] {
            assert!(linux_pid_stat_is_live(&format!("12 (ChatGPT) {} 1 2", state)));
        }
    }

    #[test]
    fn process_names_can_contain_spaces_and_closing_parentheses() {
        assert!(linux_pid_stat_is_live("12 (name ) with space) S 1 2"));
        assert!(!linux_pid_stat_is_live("12 (name ) with space) Z 1 2"));
        assert!(!linux_pid_stat_is_live(""));
        assert!(!linux_pid_stat_is_live("12 (ChatGPT) "));
    }

    #[test]
    fn unknown_identity_is_not_confirmed_absence() {
        let expected = "/managed/ChatGPT";
        assert_eq!(linux_codex_executable_matches(expected, expected, Some(expected)), Ok(true));
        assert!(linux_codex_executable_matches(expected, "", Some(expected)).is_err());
        assert!(linux_codex_executable_matches(expected, "", None).is_err());
        assert!(linux_codex_executable_matches(expected, "/other/exe", Some(expected)).is_err());
        assert_eq!(linux_codex_executable_matches(expected, "/other/ChatGPT", Some("/other/ChatGPT")), Ok(false));
    }
}
