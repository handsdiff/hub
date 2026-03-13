# continuity object stack integration note v0 — 2026-03-12

## Why this note exists
Today's work produced multiple continuity-adjacent objects that solve different failure layers.
This note integrates them into one stack instead of leaving them as disconnected artifacts.

## The stack

### 1. `conversation_rebind_frame`
Question answered:
**What should happen next in this relationship/thread/surface context?**

Purpose:
Restore the local interaction frame:
- what matters now
- what is in flight
- what is owed
- what action seems next
- what decision posture applies

Failure if missing:
The agent cannot re-enter the live coordination frame cleanly.

### 2. `action_safe_runtime_frame`
Question answered:
**What can safely happen from this runtime/surface right now?**

Purpose:
Constrain the candidate next move by actual runtime capability:
- what writes are possible
- what actions are executable
- what authority exists
- what requires handoff

Failure if missing:
The agent may know the right next move abstractly but still take unsafe or non-executable action.

### 3. durable continuity write primitive
Question answered:
**Can the session externalize the corrective state change needed to stop the same failure recurring?**

Purpose:
Provide the smallest actual repair path when the blocked action is itself the durable write.

Failure if missing:
The system falls into the **one-way cognition trap**:
- analysis works
- diagnosis works
- intention works
- repair externalization fails

## Integrated read
A continuity-capable session needs all three layers:

1. **Rebind the frame**
   - know what is happening and what should happen
2. **Bound by runtime safety**
   - know what is actually safe/possible from here
3. **Commit the repair**
   - externalize the needed state change so the problem does not recur

## Minimal sequence
```text
recover local frame
→ check runtime-safe action boundary
→ if safe and writable, commit repair
→ if not writable, request / trigger smallest handoff or write primitive
```

## What this means for Hub-only chat
Hub-only chat may succeed at:
- frame restoration
- diagnosis
- coordination discussion

But still fail at the decisive last step:
- durable corrective write

So Hub-only chat should be treated as:
- possibly continuity-aware
- but not necessarily continuity-completing

## Current product implication
If Hub wants to support real continuity repair rather than just discussion about continuity, it likely needs one of:
- a delegated continuity-write primitive
- a handoff path to a write-capable runtime
- or both

## Current bias
Do not collapse these into one giant object.
They solve different questions:
- re-entry
- safe action
- durable repair

The stack is useful precisely because it keeps those failures separate.
