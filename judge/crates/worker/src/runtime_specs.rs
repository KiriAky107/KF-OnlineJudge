//! Ordinary v2 admission and closed specifications. This module neither reads
//! user files nor executes source; every stage still goes through Root gates.
use crate::pipeline::{compiler_spec, gnu_compiler_spec, limits, verify};
use kf_contracts::{judging as v1, judging_v2 as v2, judging_versions as versions};
use kf_engine::diagnostic::{OptimizationLevel, OptimizationRequest};
use kf_sandbox::spec::{ExecutionProfile as P, ExecutionSpec, InputArtifact, SpecPolicy};
use sha2::{Digest as _, Sha256};

/// A canonical manifest and lease checked against the operator's capability
/// registry. Private fields prevent widening budgets after admission.
pub struct AuthorizedJob {
    manifest: v2::JobManifest,
    binding: v1::LeaseBinding,
    manifest_digest: v1::Digest,
}
impl AuthorizedJob {
    pub fn admit(
        manifest: v2::JobManifest,
        binding: v1::LeaseBinding,
        manifest_digest: v1::Digest,
        attempt_number: u32,
        policy: &versions::NodeAdmission,
    ) -> Result<Self, &'static str> {
        binding.validate()?;
        if binding.evaluation_id != manifest.evaluation_id {
            return Err("lease differs from authorized manifest");
        }
        policy.admit(&versions::JobManifest::V2(manifest.clone()), attempt_number)?;
        let canonical = v2::manifest_bytes(&manifest)?;
        if format!("{:x}", Sha256::digest(canonical)) != manifest_digest.as_str() {
            return Err("canonical manifest digest differs from lease");
        }
        Ok(Self {
            manifest,
            binding,
            manifest_digest,
        })
    }
    pub fn manifest(&self) -> &v2::JobManifest {
        &self.manifest
    }
    pub fn binding(&self) -> &v1::LeaseBinding {
        &self.binding
    }
    pub fn manifest_digest(&self) -> &v1::Digest {
        &self.manifest_digest
    }
    pub fn preparation_spec(&self, run_id: String) -> Result<ExecutionSpec, &'static str> {
        let m = &self.manifest;
        let spec = match m.language {
            v2::Language::C17 | v2::Language::Cpp20 => {
                let mut spec = compiler_spec(
                    if m.language == v2::Language::C17 {
                        v1::Language::C17
                    } else {
                        v1::Language::Cpp20
                    },
                    "source".into(),
                    run_id,
                    &m.compile_limits,
                );
                spec.runtime_identity = Some(m.toolchain.clone());
                spec
            }
            v2::Language::Python313 | v2::Language::Java21 => {
                let profile = if m.language == v2::Language::Python313 {
                    P::PythonCheck
                } else {
                    P::JavaCompile
                };
                let budget = limits(&m.compile_limits);
                ExecutionSpec {
                    schema_version: kf_contracts::SchemaVersion,
                    runtime_identity: Some(m.toolchain.clone()),
                    run_id,
                    profile,
                    argv: kf_sandbox::profiles::argv(profile, &budget)?,
                    env: Default::default(),
                    cwd: ".".into(),
                    inputs: vec![InputArtifact {
                        artifact_id: "source".into(),
                        path: m.language.contract().source_name.into(),
                    }],
                    stdin_artifact: None,
                    outputs: vec![],
                    limits: budget,
                }
            }
        };
        checked(spec)
    }
    /// Metadata is only accepted for this source/runtime/attempt. The adapter
    /// must separately verify every sealed file's byte count and digest.
    pub fn validate_program(&self, program: &v2::CompiledProgram) -> v1::Validation {
        program.validate()?;
        let m = &self.manifest;
        if program.binding != self.binding
            || program.source != m.source
            || program.toolchain != m.toolchain
            || program.kind != m.language.contract().program_kind
        {
            return Err("program differs from authorized source, runtime or lease");
        }
        if program.kind != v2::ProgramKind::PythonSource
            && (program
                .files
                .iter()
                .any(|f| f.artifact.bytes > m.compile_limits.file_bytes)
                || program.bytes() > m.compile_limits.workdir_bytes)
        {
            return Err("program exceeds preparation output budget");
        }
        Ok(())
    }
    pub fn run_spec(
        &self,
        program: &v2::CompiledProgram,
        case_index: usize,
        run_id: String,
    ) -> Result<ExecutionSpec, &'static str> {
        self.validate_program(program)?;
        if case_index >= self.manifest.cases.len() {
            return Err("test point is outside authorized manifest");
        }
        let profile = match self.manifest.language {
            v2::Language::C17 | v2::Language::Cpp20 => P::Native,
            v2::Language::Python313 => P::PythonRun,
            v2::Language::Java21 => P::JavaRun,
        };
        let budget = limits(&self.manifest.run_limits);
        checked(ExecutionSpec {
            schema_version: kf_contracts::SchemaVersion,
            runtime_identity: Some(self.manifest.toolchain.clone()),
            run_id,
            profile,
            argv: if profile == P::Native {
                vec!["/work/main".into()]
            } else {
                kf_sandbox::profiles::argv(profile, &budget)?
            },
            env: Default::default(),
            cwd: ".".into(),
            inputs: program
                .files
                .iter()
                .enumerate()
                .map(|(i, f)| InputArtifact {
                    artifact_id: program_artifact_id(i),
                    path: f.path.clone(),
                })
                .collect(),
            stdin_artifact: Some("stdin".into()),
            outputs: vec![],
            limits: budget,
        })
    }
}
pub(crate) fn program_artifact_id(index: usize) -> String {
    format!("program-{index:03}")
}
fn checked(spec: ExecutionSpec) -> Result<ExecutionSpec, &'static str> {
    spec.clone()
        .validate(&SpecPolicy::default())
        .map_err(|_| "invalid fixed runtime specification")?;
    Ok(spec)
}

/// Construct a compile stage for the diagnostic worker from the admitted task.
/// This only binds inputs; execution must still obtain approval from Root.
pub fn diagnostic_preparation_spec(
    request: &OptimizationRequest<'_>,
    level: OptimizationLevel,
    run_id: String,
    source: &[u8],
) -> Result<ExecutionSpec, &'static str> {
    use OptimizationLevel::{O0, O2, Ubsan};

    let config = request.configuration;
    let profile = match (config.language, level) {
        (v2::Language::C17, O0) => P::CompileCDiagnostic,
        (v2::Language::Cpp20, O0) => P::CompileCppDiagnostic,
        (v2::Language::C17, O2) => P::CompileC,
        (v2::Language::Cpp20, O2) => P::CompileCpp,
        (v2::Language::C17, Ubsan) => P::CompileCUbsan,
        (v2::Language::Cpp20, Ubsan) => P::CompileCppUbsan,
        _ => return Err("diagnostic compilation requires C17 or C++20"),
    };
    config.validate()?;
    config.compile_limits.validate()?;
    request.binding.validate()?;
    request.source.validate()?;
    if request.source.bytes == 0 || request.source.bytes > v1::MAX_SOURCE_BYTES {
        return Err("invalid diagnostic source size");
    }
    verify(request.source, source)
        .map_err(|_| "diagnostic source differs from admitted artifact")?;
    let mut spec = gnu_compiler_spec(profile, "source".into(), run_id, &config.compile_limits)
        .ok_or("invalid diagnostic compiler profile")?;
    spec.runtime_identity = Some(config.toolchain.clone());
    Ok(spec)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn fixture<T: serde::de::DeserializeOwned>(name: &str) -> T {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../../tests/fixtures/contracts-v2")
            .join(format!("{name}.json"));
        v1::decode_wire(&std::fs::read(path).unwrap()).unwrap()
    }
    fn authorized(language: &str) -> (AuthorizedJob, v2::CompiledProgram) {
        let m: v2::JobManifest = fixture(&format!("judge-manifest.valid-{language}"));
        let report: v2::JudgementReport = fixture(&format!("judge-result.valid-{language}"));
        let policy: versions::NodeAdmission = fixture("judge-registry.valid");
        let digest = format!("{:x}", Sha256::digest(v2::manifest_bytes(&m).unwrap()))
            .try_into()
            .unwrap();
        let job = AuthorizedJob::admit(m, report.binding, digest, 1, &policy).unwrap();
        (job, report.compilation.program.unwrap())
    }
    #[test]
    fn diagnostic_compile_specs_share_source_pin_and_compile_budget() {
        let (job, _) = authorized("cpp20");
        let m = job.manifest();
        let configuration = v2::LanguageConfiguration {
            language: m.language,
            toolchain: m.toolchain.clone(),
            compile_limits: m.compile_limits.clone(),
            run_limits: m.run_limits.clone(),
            job_wall_time_us: m.job_wall_time_us,
        };
        let source = b"synthetic source fixture";
        let artifact = v1::ArtifactReference {
            digest: format!("{:x}", Sha256::digest(source)).try_into().unwrap(),
            bytes: source.len() as u64,
        };
        let request = OptimizationRequest {
            configuration: &configuration,
            binding: job.binding(),
            source: &artifact,
            public_sample: &m.cases[0],
            expected: b"",
        };
        let o0 = diagnostic_preparation_spec(&request, OptimizationLevel::O0, "o0".into(), source)
            .unwrap();
        let o2 = diagnostic_preparation_spec(&request, OptimizationLevel::O2, "o2".into(), source)
            .unwrap();
        assert_eq!(o0.profile, P::CompileCppDiagnostic);
        assert_eq!(o2.profile, P::CompileCpp);
        assert_eq!((o0.argv[2].as_str(), o2.argv[2].as_str()), ("-O0", "-O2"));
        assert_eq!(o0.runtime_identity.as_ref(), Some(&configuration.toolchain));
        assert_eq!(o0.runtime_identity, o2.runtime_identity);
        assert_eq!(o0.inputs, o2.inputs);
        assert_eq!(o0.limits, o2.limits);
        assert!(o0.stdin_artifact.is_none());
        let mut different = *source;
        different[0] = b'x';
        assert!(
            diagnostic_preparation_spec(&request, OptimizationLevel::O0, "o0".into(), &different)
                .is_err()
        );
    }

    #[test]
    fn all_four_languages_use_exact_pins_fixed_commands_and_private_points() {
        for (language, prepare, run) in [
            ("c17", P::CompileC, P::Native),
            ("cpp20", P::CompileCpp, P::Native),
            ("python313", P::PythonCheck, P::PythonRun),
            ("java21", P::JavaCompile, P::JavaRun),
        ] {
            let (j, program) = authorized(language);
            let p = j.preparation_spec("prepare".into()).unwrap();
            assert_eq!(p.profile, prepare);
            assert_eq!(p.runtime_identity.as_ref(), Some(&j.manifest.toolchain));
            assert!(p.env.is_empty());
            assert!(p.stdin_artifact.is_none());
            assert_eq!(p.inputs[0].path, j.manifest.language.contract().source_name);
            for index in 0..j.manifest.cases.len() {
                let r = j
                    .run_spec(&program, index, format!("point-{index}"))
                    .unwrap();
                assert_eq!(r.profile, run);
                assert_eq!(r.runtime_identity, p.runtime_identity);
                assert_eq!(r.limits, limits(&j.manifest.run_limits));
                assert!(r.env.is_empty() && r.outputs.is_empty());
                assert_eq!(r.stdin_artifact.as_deref(), Some("stdin"));
                assert_eq!(r.inputs.len(), program.files.len());
                assert!(
                    r.inputs
                        .iter()
                        .all(|f| f.artifact_id.starts_with("program-"))
                );
                // Expected answers, source paths and manifest host paths are not inputs.
                assert_eq!(
                    r.inputs.iter().map(|f| &f.path).collect::<Vec<_>>(),
                    program.files.iter().map(|f| &f.path).collect::<Vec<_>>()
                );
            }
            assert!(
                j.run_spec(&program, j.manifest.cases.len(), "point".into())
                    .is_err()
            );
        }
    }
    #[test]
    fn lease_manifest_digest_runtime_and_operator_capabilities_are_fenced() {
        let (j, _) = authorized("python313");
        let policy: versions::NodeAdmission = fixture("judge-registry.valid");
        let admit = |m, b, d, n| AuthorizedJob::admit(m, b, d, n, &policy);
        let mut wrong = j.manifest.clone();
        wrong.toolchain.image_digest = "f".repeat(64).try_into().unwrap();
        assert!(admit(wrong, j.binding.clone(), j.manifest_digest.clone(), 1).is_err());
        let mut b = j.binding.clone();
        b.evaluation_id = "aaaaaaaa-aaaa-4aaa-8aaa-000000001000"
            .to_string()
            .try_into()
            .unwrap();
        assert!(admit(j.manifest.clone(), b, j.manifest_digest.clone(), 1).is_err());
        assert!(
            admit(
                j.manifest.clone(),
                j.binding.clone(),
                "f".repeat(64).try_into().unwrap(),
                1
            )
            .is_err()
        );
        assert!(
            admit(
                j.manifest.clone(),
                j.binding.clone(),
                j.manifest_digest.clone(),
                4
            )
            .is_err()
        );
        let mut wrong = j.manifest.clone();
        wrong.run_limits.memory_bytes += 1;
        assert!(admit(wrong, j.binding.clone(), j.manifest_digest.clone(), 1).is_err());
        let mut legacy: v2::AdmissionRegistry = fixture("judge-registry.valid");
        legacy.multi_runtime = None;
        assert!(
            AuthorizedJob::admit(
                j.manifest.clone(),
                j.binding.clone(),
                j.manifest_digest.clone(),
                1,
                &versions::NodeAdmission::Registry(legacy)
            )
            .is_err()
        );
    }
    #[test]
    fn sealed_program_metadata_cannot_change_source_attempt_type_or_budget() {
        for language in ["c17", "python313", "java21"] {
            let (j, p) = authorized(language);
            let mut bad = p.clone();
            bad.binding.attempt_id = "aaaaaaaa-aaaa-4aaa-8aaa-000000001000"
                .to_string()
                .try_into()
                .unwrap();
            assert!(j.validate_program(&bad).is_err());
            bad = p.clone();
            bad.source.digest = "f".repeat(64).try_into().unwrap();
            assert!(j.validate_program(&bad).is_err());
            bad = p.clone();
            bad.toolchain.image_digest = "f".repeat(64).try_into().unwrap();
            assert!(j.validate_program(&bad).is_err());
            bad = p.clone();
            bad.files[0].path = "../Main.class".into();
            assert!(j.validate_program(&bad).is_err());
            bad = p.clone();
            bad.files[0].artifact.bytes = 0;
            assert!(j.validate_program(&bad).is_err());
            if language != "python313" {
                bad = p;
                bad.files[0].artifact.bytes = j.manifest.compile_limits.file_bytes + 1;
                assert!(j.validate_program(&bad).is_err());
            }
        }
        let (j, mut p) = authorized("java21");
        p.files[0].path = "A$1.class".into();
        j.validate_program(&p).unwrap();
        p.files.push(p.files[0].clone());
        assert!(j.validate_program(&p).is_err());
    }
}
