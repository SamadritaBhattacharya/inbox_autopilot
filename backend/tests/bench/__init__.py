"""The benchmark harness — the gate every behavioural change is judged against.

`CLAUDE.md` §20 asks for "a benchmark harness over the fixture [that] reports success-rate /
steps / tokens / % terminated-with-typed-code (target 100%)". This is it.

**Why it exists at all.** Every planned improvement — trimming the prompt, scoping the
funnel, caching locators, mining strategies from trajectories — is a claim about behaviour.
Without a number to check the claim against, "the agent got smarter" is indistinguishable
from "the agent got different", and a learning system with no eval gate does not improve, it
drifts.

**It measures the SYSTEM, not the model.** The default run scripts the `LLMClient`, so model
output is fixed and every number reflects the graph, the guards, the funnel, and the
dispatcher. That is deliberate: it makes the harness hermetic, free, and deterministic enough
to fail CI honestly. Measuring *model* quality needs a real provider and belongs behind the
existing `live` marker.

**Success is judged by contract, never by self-report.** A run counts as successful when the
actions it actually dispatched match what the task required — not when the agent called
`Complete(success=True)`. An agent grading its own homework is not an evaluation.
"""
