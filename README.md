## cuhkvoting

Minimal CLI to browse arXiv and vote on papers, with paper data and votes stored in GitHub under `papers/`.

### Install

```bash
pip install git+https://github.com/gravityhub-org/cuhkvoting.git
```

### Quick setup

For read-only commands (`today`, `search`, `topvoted`) you do not need auth.

For `vote`, authenticate once:

```bash
gh auth login
```

or export a token:

```bash
export GITHUB_TOKEN=...
```

If running outside a local git clone, set target repo:

```bash
export CUHKVOTING_REPO=gravityhub-org/cuhkvoting
```

Optional branch override:

```bash
export CUHKVOTING_BRANCH=main
```

### Commands

```bash
cuhkvoting today
cuhkvoting search "vision language model"
cuhkvoting topvoted --N 10
cuhkvoting vote 2504.12345
```

### Data format

Votes and metadata are stored as JSON files:

- `papers/<arxiv_id>.json`
- one file per paper
- one vote per GitHub username enforced by CLI
