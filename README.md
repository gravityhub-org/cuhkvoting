## cuhkvoting

Minimal CLI to browse arXiv and vote on papers, with paper data and votes stored in GitHub under `papers/`.

### Install

```bash
pip install --upgrade git+https://github.com/gravityhub-org/cuhkvoting.git && cuhkvoting --install-completion
```

Or:

```bash
uv tool install --upgrade git+https://github.com/gravityhub-org/cuhkvoting.git && cuhkvoting --install-completion
```


### Quick setup

For read-only commands (`today`, `search`, `topvoted`) you do not need auth.

No GitHub CLI is required.

Auth behavior:

- `today`, `search`, `topvoted`: works without auth
- `vote`: needs write auth via either:
  - token (`CUHKVOTING_TOKEN` or `GITHUB_TOKEN`), or
  - git SSH key for `git@github.com`

Set vote identity (optional if global git `user.name` is already set):

```bash
export CUHKVOTING_USER=your-github-username
```

Optional SSH check:

```bash
ssh -T git@github.com
```

Default repo is `gravityhub-org/cuhkvoting-records`.

If you want override default (or run against another repo):

```bash
export CUHKVOTING_REPO=gravityhub-org/cuhkvoting-records
```

Optional branch override:

```bash
export CUHKVOTING_BRANCH=main
```

### Commands

```bash
cuhkvoting today
cuhkvoting today "black hole"
cuhkvoting today lensing gravitational waves
cuhkvoting lastweek
cuhkvoting lastweek "black hole"
cuhkvoting lastweek lensing gravitational waves
cuhkvoting search "vision language model"
cuhkvoting topvoted --N 10
cuhkvoting record
cuhkvoting vote 2504.12345
cuhkvoting vote remove 2504.12345
cuhkvoting select 2504.12345
cuhkvoting admin trash 2504.12345
```

### Bash autocomplete

```bash
cuhkvoting --install-completion
```

If you prefer manual setup:

```bash
eval "$(_CUHKVOTING_COMPLETE=bash_source cuhkvoting)"
```

### Data format

Votes and metadata are stored as JSON files:

- `papers/<arxiv_id>.json`
- one file per paper
- one vote per GitHub username enforced by CLI
- votes expire after 6 months
