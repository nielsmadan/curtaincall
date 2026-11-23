# Changelog

## v2.2.0

### Features

- Update policy pins during releases


### Maintenance

- Bump the non-breaking group with 2 updates (#8)

## v2.1.0

### Features

- Add pins command for other repositories' release assets


### Maintenance

- Update docs

## v2.0.1

### Fixes

- Sort test imports with tests as a first-party package (#7)

## v2.0.0

### Features

- Rename policy keys and restructure the release engine


### Maintenance

- Inject tool lookup into doctor checks

- Harden CI and release workflows

## v1.4.0

### Features

- Improve release error reporting

## v1.3.1

### Fixes

- Explain release history check failures

## v1.3.0

### Features

- Add sign policy option for signed release commits and tags


### Fixes

- Lock the repository's own action in workflows


### Maintenance

- Bump ruff (#6)

- Keep release retries from downgrading the tap

- Lock workflow actions by tag

## v1.2.0

### Features

- Add release confirmation summary


### Maintenance

- Auto-merge dependabot patch and minor updates

## v1.1.0

### Features

- Add tag and tag_prefix stage placeholders

- Add curtains ci commands and a GitHub Action to run them


### Maintenance

- Bump ruff (#1)

- Bump ruff (#3)

- Bump the non-breaking group with 2 updates (#2)

- Bump the non-breaking group with 3 updates (#5)

- Bump astral-sh/setup-uv (#4)

## v1.0.0

### Features

- Add curtains release CLI

- Add self-release automation

- Document installing curtains from GitHub Releases

- Publish a Homebrew formula with each release


### Maintenance

- Target 1.0.0 and run each release gate once

- Stage the release bundle outside the checkout and require notes

- Note reproducible rebuilds for release retries

