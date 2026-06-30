Last reviewed: 2026-06-30
Status: Current
Owner area: operations

# BroadLink Restart Recovery Playbook

This document records the troubleshooting pattern that worked for the recent
BroadLink instability on the Mac mini OpenClaw stack.

## Scope

Use this playbook when all of the following are true:

- BroadLink device discovery sometimes works, but actual IR send/auth is flaky.
- A short-lived manual command works more often than the long-running bot/web path.
- Restarting the stack can temporarily improve or worsen the symptom.

This is not a generic "relearn IR codes" guide. The point here is to separate
device/network problems from process-context problems.

## The key lesson

For this incident, the decisive signal was:

- fresh short-lived `ir_worker` process: success
- long-running bridge / Telegram path: intermittent `No route to host` during auth

That means the device itself was not the primary suspect. The bigger problem was
that BroadLink UDP auth was sensitive to startup context and stale process state.

## What to verify first

Run these checks in order before changing code:

1. Confirm the target device is reachable on the local network.
2. Confirm `discover` sees the device.
3. Confirm a fresh short-lived worker can `auth` and send one harmless IR action.
4. Compare that result with the long-running bridge / Telegram path.

If step 3 succeeds but step 4 fails, treat it as a runtime-context problem, not
an IR-payload problem.

## Signals that mattered

These were high-signal checks:

- `ping <device-ip>` succeeds.
- ARP table shows the expected MAC for the device.
- route lookup shows the expected local interface.
- a direct Python probe can `discover()` and `auth()`.
- `openclaw_adapter.ir_worker send ...` succeeds.
- web/bridge `/ir` path still reports BroadLink auth failure or `No route to host`.

This combination means:

- local L2/L3 reachability is present
- device power/network is probably fine
- the failing layer is likely process context, stale socket state, or how the
  long-running service was started

## Things that did not explain the failure

Do not jump to these too early:

- relearning IR codes
- changing command wording
- assuming the RM4 is offline because one path failed
- assuming discovery success implies send/auth success

The important distinction is broadcast discovery vs later unicast auth/send.

## Working hypothesis that matched reality

BroadLink auth on this Mac was sensitive to:

- which process context launched the worker
- whether the stack inherited stale tmux / Terminal / service state
- whether the first auth attempt happened in a fresh short-lived process

That is why manual `ir_worker` success was more trustworthy than the already
running bridge's internal state.

## Recovery approach that worked

We adopted two rules:

1. BroadLink actions should keep using a short-lived worker process.
2. Every cold start and `/restartall` should run a BroadLink preflight before
   bringing Telegram and command bridge fully back up.

The preflight does this:

1. start a fresh process
2. run `openclaw_adapter.ir_worker discover`
3. retry a few times
4. only then start the long-running BroadLink-sensitive services

This does not "fix" every network problem. It specifically reduces failures
caused by stale startup context.

## Why preflight belongs before Telegram and bridge

Telegram and command bridge are the paths that later trigger `/ir`.

If they come up first, they may become the first BroadLink-touching process and
inherit the bad state we were trying to avoid. Running preflight first gives the
stack one clean auth/discovery pass from a fresh process before those services
start serving user requests.

## Where the fix lives

Current wiring:

- cold start: `launchers/start-mac-mini-stack.command`
- live restart: `src/openclaw_adapter/service_restart.py`

Both now run BroadLink preflight before Telegram and command bridge startup.

## Verification that matters after a fix

Do not stop at unit tests. Verify all three:

1. script generation / ordering tests pass
2. full local stack restart shows BroadLink preflight in logs
3. after restart, the bridge `/ir` path can successfully send an IR command

For this incident, that final bridge-path verification was the acceptance gate.

## When to suspect a different problem

Use another branch of investigation if:

- fresh `ir_worker` also fails consistently
- `discover` cannot find the device at all
- ARP/route checks fail
- multiple local processes fail identically, including the shortest direct probe

That usually points back to the device, Wi-Fi/LAN routing, VPN/firewall, or a
real local-network outage.

## Operational rule going forward

If BroadLink starts failing again, first answer this question:

"Does a fresh short-lived worker still succeed?"

If yes, debug startup context and restart path first.
If no, debug the network/device path first.
