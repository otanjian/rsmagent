//! CLI around the launcher.
//!
//! Two modes, because they answer different questions:
//!
//! * **JSON mode** (no flag): one job on stdin, one result on stdout. For callers
//!   that want the launcher to capture and bound the output.
//! * **wrap mode** (`--wrap <exe> [args...]`): run a command under a Job Object and
//!   pass this process's own stdio through untouched. This is what the desktop
//!   needs, because the process it launches is a long-lived JSON-RPC peer: anything
//!   the wrapper did to those streams would break the session.
//!
//! Exit status follows the same rule in both modes: a *refusal* is non-zero, so a
//! caller cannot mistake "we could not confine it" for a result. Wrap mode
//! otherwise reports the wrapped command's own exit code, since replaying it is the
//! whole reason to have a supervisor.

use std::io::Read;

use script_launcher::{run, JobSpec};

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();

    if args.first().map(|a| a.as_str()) == Some("--wrap") {
        wrap_main(&args[1..]);
    }

    let mut input = String::new();
    if let Err(err) = std::io::stdin().read_to_string(&mut input) {
        fail(&format!("could not read the job from stdin: {err}"));
    }
    let job: JobSpec = match serde_json::from_str(&input) {
        Ok(job) => job,
        Err(err) => fail(&format!("the job is not valid JSON: {err}")),
    };
    match run(&job) {
        Ok(result) => match serde_json::to_string(&result) {
            Ok(body) => println!("{body}"),
            Err(err) => fail(&format!("could not serialise the result: {err}")),
        },
        Err(err) => fail(&err.to_string()),
    }
}

/// `--wrap <exe> [args...]`, with the working directory, environment and limits
/// taken from the environment.
///
/// Configuration travels in the environment rather than in more flags so the
/// command line stays exactly the wrapped command: a wrapper that parsed its
/// child's arguments would have to know which of them were its own.
#[cfg(windows)]
fn wrap_main(args: &[String]) {
    use script_launcher::wrap::{run_wrapped, wrap_environment};

    let (executable, command_args) = match args.split_first() {
        Some((exe, rest)) => (exe.clone(), rest.to_vec()),
        None => fail("--wrap needs an executable to run"),
    };
    let cwd = match std::env::var("COW_WRAP_CWD") {
        Ok(value) if !value.is_empty() => value,
        // Refusing beats guessing: a wrapper that chose its own working directory
        // would run the child somewhere the caller did not pick.
        _ => fail("COW_WRAP_CWD is required: the wrapper does not choose a working directory"),
    };
    let limits = script_launcher::Limits {
        timeout_ms: std::env::var("COW_WRAP_TIMEOUT_MS").ok().and_then(|v| v.parse().ok()),
        max_processes: std::env::var("COW_WRAP_MAX_PROCESSES").ok().and_then(|v| v.parse().ok()),
        max_memory_bytes: std::env::var("COW_WRAP_MAX_MEMORY").ok().and_then(|v| v.parse().ok()),
    };

    // The wrapped command's environment is declared through COW_WRAP_ENV, one
    // `NAME=VALUE` per line. Passing this process's environment through would defeat
    // the whitelist the caller is here to express.
    let mut declared = Vec::new();
    if let Ok(blob) = std::env::var("COW_WRAP_ENV") {
        for line in blob.lines().filter(|l| !l.trim().is_empty()) {
            if let Some((name, value)) = line.split_once('=') {
                declared.push((name.to_string(), value.to_string()));
            }
        }
    }
    let env = wrap_environment(&declared);

    // The access boundary, chosen by the caller's environment for the same reason
    // the limits are: the command line stays exactly the wrapped command, so this
    // wrapper never has to parse its child's arguments.
    let access = match std::env::var("COW_WRAP_ACCESS").unwrap_or_default().as_str() {
        "app_container" => script_launcher::Access::AppContainer,
        _ => script_launcher::Access::Inherited,
    };
    // An AppContainer cannot inherit this process's environment, and the ones below
    // are not optional: without `LOCALAPPDATA`/`APPDATA` the child fails to start
    // with ERROR_ENVVAR_NOT_FOUND (203) -- the same wall the JSON mode hit. They are
    // added only for the confined path, so an unconfined wrap keeps exactly the
    // environment its caller declared.
    let mut env = env;
    if access == script_launcher::Access::AppContainer {
        for name in ["LOCALAPPDATA", "APPDATA", "USERPROFILE"] {
            if env.iter().any(|(existing, _)| existing.eq_ignore_ascii_case(name)) {
                continue;
            }
            if let Ok(value) = std::env::var(name) {
                env.push((name.to_string(), value));
            }
        }
    }

    // Directories the run needs beyond its working directory, one per line. The
    // desktop puts the run's temp directory outside the project, so a confined run
    // cannot write there without this.
    let extra_grants: Vec<String> = std::env::var("COW_WRAP_GRANT_EXTRA")
        .unwrap_or_default()
        .lines()
        .map(|line| line.trim().to_string())
        .filter(|line| !line.is_empty())
        .collect();

    match run_wrapped(&executable, &command_args, &cwd, &env, limits, access, &extra_grants) {
        Ok((_result, exit_code)) => std::process::exit(exit_code as i32),
        Err(err) => {
            // Reported on stderr, never stdout: the caller's stdout belongs to the
            // wrapped protocol, and an error written there would corrupt it.
            eprintln!("script-launcher refused to wrap: {err}");
            std::process::exit(2)
        }
    }
}

#[cfg(not(windows))]
fn wrap_main(_args: &[String]) {
    eprintln!("script-launcher refused to wrap: this mode is Windows-only");
    std::process::exit(2)
}

fn fail(message: &str) -> ! {
    let body = serde_json::json!({ "refused": message });
    println!("{body}");
    std::process::exit(2)
}
