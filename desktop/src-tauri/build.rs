fn main() {
    // ANX_RUNTIME_ARCHIVE names the runtime zip to embed; MCPM_RUNTIME_ARCHIVE
    // is the legacy name, still honoured.
    println!("cargo:rerun-if-env-changed=ANX_RUNTIME_ARCHIVE");
    println!("cargo:rerun-if-env-changed=MCPM_RUNTIME_ARCHIVE");
    let source = std::env::var_os("ANX_RUNTIME_ARCHIVE").filter(|value| !value.is_empty())
        .or_else(|| std::env::var_os("MCPM_RUNTIME_ARCHIVE").filter(|value| !value.is_empty()))
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| std::path::PathBuf::from("../../artifacts/agent-nexus-runtime.zip"));
    let source = source.canonicalize().expect("Generate the runtime archive with make runtime first");
    println!("cargo:rerun-if-changed={}", source.display());
    println!("cargo:rustc-env=ANX_RUNTIME_ARCHIVE_PATH={}", source.display());
    tauri_build::build();
    // Tauri links its Windows manifest into binaries only. The native
    // lifecycle example also needs Common Controls v6 before main() runs.
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("windows")
        && std::env::var("CARGO_CFG_TARGET_ENV").as_deref() == Ok("msvc") {
        let resource = std::path::PathBuf::from(std::env::var_os("OUT_DIR").unwrap())
            .join("resource.lib");
        println!("cargo:rustc-link-arg-examples={}", resource.display());
    }
}
