# Single-node prerelease closeout

## Verified implementation

The graduation-project prerelease implementation is
`b519bfd5d6221f3eb886bb054913f598d1fd46bd`. Its offline bundle manifest SHA256 is
`32eed382051766bdd01ba57332fa10eab1f3dcff535f53db70c885c7a5f976f4`
(8,572 files; 1,014,132,542 payload bytes). Full bundle-byte verification passed.
Later documentation commits do not change that tested bundle identity.

- 379 targeted delivery/proxy-startup/unit-snapshot tests passed. Ruff, architecture,
  contracts and real systemd unit verification passed.
- The new bundle was installed and started in an isolated disposable guest through
  public release commands. HTTPS login returned 200; secure/HttpOnly cookies,
  cross-origin refusal, CSRF logout (204), drain and stop were verified.
- An initial login attempt used the wrong synthetic fixture password and returned
  401. The retained continuation used the correct fixture in the already-installed
  guest and passed; the failed attempt remains recorded as failed.
- [Trusted engineering CI for this exact source](https://github.com/KiriAky107/KF-OnlineJudge/actions/runs/43881744782)
  completed successfully. Both configured feature-branch remotes were independently
  verified at this source before documentation closeout.

## Acceptance limits

The 42-item local plan contains 39 historically accepted items and three
explicitly scoped closeouts (F06/G05/G06), not 42 fresh full-VM acceptances.
Historical C101/B127r2 cold421 and E06-433 results remain valid only for their
recorded source/environment. They are not rerun results for this bundle.
The browser425 main driver exited successfully, but its post-stop evidence tail
was not completed after a host reboot/kernel change. That gap remains open.
This last revision did not repeat complete cold recovery, soak or fault suites.

Temporary resources from the final targeted validation were removed. Historical
VM evidence sources were retained; this is not a claim that every historical
disk has been deleted. The long-running acceptance automation remains paused.
Private plans, raw VM evidence, configurations and credentials are not published
in this repository. Existing operations instructions remain in
[the release-tool runbook](../../scripts/release/README.md).

## Production is a separate decision

Target capacity, domain/TLS, backup destination and maintenance window still need
to be selected and validated. Closing this prerelease phase and merging its
engineering branch do not constitute a formal release tag or production
deployment. No additional business features or broad historical replay are
required solely to close this graduation-project phase.
