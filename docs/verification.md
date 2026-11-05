# Verification — 5 November 2026

This release contains selected original source, not the earlier translated application draft. The snapshot manifests verify original bytes independently of private repositories.

- 288 selected original source/media files match their manifests.
- 372 Python application tests pass against the original bot/API code.
- The selected original website passes 13 unit tests and builds 15 static pages with zero Astro diagnostics (Node 26.9/npm 11). Its original package metadata still pins Node 22.22.3/npm 10.9.8; these files are preserved as source.
- All eleven database migrations are exercised twice on a disposable local PostgreSQL instance, with ten API/DB/lifecycle/access checks.
- The walkthrough captures 45 synthetic BotFlow scenarios, retaining original Russian dialogue. English controls are a presentation wrapper only.
- Public GitHub Pages was checked in a browser at 402 and 1280 pixels: no horizontal overflow or broken images; audience selection and restart work. The walkthrough makes no HTTP requests to a backend or provider.
- No production data, credentials, private VM deployment or provider acceptance is inferred from these tests.

The portable application runner explicitly selects tests for the exported application boundary. Historical operational tests referencing private VM recipes are reference source outside that runner. Website clinical media is not redistributed; the original business website is the complete visual reference.

No original source repository or business service is modified by publication. See publication-boundary.md.

The original website lockfile has known dependency advisories: the 5 November production-dependency audit reports five high and one moderate advisory. The source snapshot is for inspection and local evaluation, with no production-readiness or security-audit claim. GitHub Pages serves only the generated bot walkthrough; it does not run the website's build dependencies or a public application server. Dependency remediation in the original product is outside this publication.
