# Contributing

Use a synthetic project when showing a journal entry or a failed check. For a behavior change, explain which user action changes and how to verify it. For a substantial change, link the maintainer-approved issue or discussion in your pull request.

Run the same checks as CI from the repository root:

```sh
python3 -B scripts/check-docs.py
python3 -B -m unittest discover -s tests
```

Sign off commits with `git commit -s` as required by the [Developer Certificate of Origin](DCO.md). The repository follows the [Contributor Covenant](CODE_OF_CONDUCT.md). Use the [security policy](SECURITY.md) for private reports.
