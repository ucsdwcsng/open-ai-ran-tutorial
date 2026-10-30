# Trained PPO schedulers

Copies of the three PPO runs reported in `docs/edgeric-rt.md`. The originals are in `edgeric-rt/runs/`, which is
gitignored. Each run was trained on the testbed with `muapps/train_ppo.py`:
- 150 iterations of 5,000 applied actions each;
- one UE per slot (`--action single`, a categorical policy);
- the permutation-equivariant "set" network (`--arch set`).

| Run | Scenario | Trace table | Traffic | γ | Evaluation (60 s, gNB / UDP Mbit/s) |
|---|---|---|---|---|---|
| `ppo_walk4` | 4 UEs, CQI 4–15 walks | `duranta-oai-ue/ue_traces_cqi_4ue.conf` | full-buffer UDP, 100 Mbit/s per UE | 0.9 | 40.80 / 36.89 (best CQI 41.54 / 37.85) |
| `ppo_walk2` | 2 UEs, CQI 4–15 walks | `duranta-oai-ue/ue_traces_cqi_s1.conf` | full-buffer UDP, 100 Mbit/s per UE | 0.9 | 28.66 / 23.91 (max CQI 29.26 / 25.02) |
| `ppo_load8844` | 4 UEs, same walks as `ppo_walk4` | `duranta-oai-ue/ue_traces_cqi_4ue.conf` | plain UDP at 8/8/4/4 Mbit/s (`traffic_nue.sh --plain`) | 0.98 | 18.42 / 17.50 (next best: fixed shares, 16.36 / 15.43) |

## Files in each run

| File | What it is |
|---|---|
| `last.npz` | The policy after the last iteration. This is the file every evaluation used. |
| `best.npz` | The policy with the best deterministic evaluation during training. |
| `ckpt.pt` | PyTorch actor, critic and optimizer state, for resuming training. |
| `train.csv` | One row per iteration: throughput, DL HIT %, losses, entropy, best-CQI share and efficiency. |
| `train_curve.png`, `train_curve.svg` | The training curve, from `tools/plot_training.py`. |
| `convergence.json` | The convergence check from `tools/plot_training.py`. |

The `.npz` files are what the muApp runs. Inference is numpy-only, about 70 µs per decision. Each file also stores:
- `n_ues`;
- `kind` (`cat`);
- `arch` (`set`);
- `meta`: the training arguments.

## Use

Run a model as the scheduler. Paths are relative to `edgeric-rt/`, which is `/edgeric` in the edgeric container:

```bash
bash scripts/edgeric_muapp.sh start sched --scheduler rl --model models/ppo_walk4/last.npz
bash scripts/edgeric_muapp.sh ctl "scheduler rl models/ppo_load8844/last.npz"     # live switch
```

The set network scores each UE from its own features and the mean and max over all UEs, so a model also runs with
a different number of UEs. The `walk` models were only evaluated on the UE counts they were trained for.

To resume training a model:
1. copy its `ckpt.pt` back to `runs/<run>/`;
2. run `train_ppo.py --tag <run> --resume`.
