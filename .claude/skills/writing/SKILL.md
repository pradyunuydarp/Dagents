---
name: writing
description: Plain-language rules for all text in Dagents — the framework site, the demo UIs, READMEs and docs, the learning guide in docs/learn, code comments, commit messages and pull request text. Use before writing or editing any of these, and when reviewing text someone else wrote.
---

# Writing in Dagents

Write in easy, professional English. Assume the reader is new to federated
learning, to data governance, or to English. Each sentence should help them
understand something or do something.

These rules apply to everything a person reads: UI text, documentation, the
learning guide, code comments, commit messages and pull request descriptions.

## 1. Rules

1. **Short sentences and plain words.** One idea per sentence. Prefer "use" to
   "leverage", "start" to "spin up", "show" to "surface".
2. **Facts and numbers instead of adjectives.** "The first request takes about
   20 seconds" tells the reader more than "the first request can be slow".
3. **Define a term the first time you use it**, or link to its definition. The
   glossary is in `docs/learn/README.md`.
4. **Descriptive headings.** A heading says what the section covers: "Plan a
   federated round", "Where the APIs are".
5. **Lists and tables** for steps, options and comparisons.
6. **State a limitation once, plainly, where it applies.** Do not repeat it or
   apologize for it.
7. **Active voice.** "The Guard denies the request", not "the request is denied".
8. **UI text is a label or an instruction.** "Choose a sample and select
   Generate SQL." Name controls by the words on the screen.
9. **Comments explain why the code is the way it is.** Mention a past mistake
   only when it stops someone repeating it, and then in one sentence.
10. **Commit messages:** a short imperative summary line ("Add the learning
    guide"), then plain sentences on what changed and why.

## 2. Avoid

| Avoid | Example | Write instead |
|---|---|---|
| Slogans built on "X, not Y" | "A framework, not a product backend." | "Dagents is a framework that other applications use." |
| Aphorisms and dramatic lines | "An enforcement layer whose absence grants access is not one." | "If the planner cannot be reached, the Guard denies the request." |
| Metaphors and figurative verbs | "load-bearing", "a warning label", "self-refuting", "a red mark to chase" | Name the actual property or behaviour. |
| Talking about honesty or importance | "honest", "honestly", "crucially", "the point is", "the whole point" | State the fact. |
| Filler | "simply", "just", "really", "actually", "very", "quite" | Delete the word. |
| Code with feelings or intentions | "the page refuses to lie", "the API is asking to be trusted" | "The page shows an error." |
| Long sentences joined by dashes and semicolons | — | Split them into two or three sentences. |
| Rhetorical questions and setups | "So what does this mean? It means…" | Say what it means. |

A contrast is fine when it carries information: "Cell and row requests are not
checked against the cohort minimum." A slogan is a contrast used for effect.

## 3. Before you finish

- **Read it once as a newcomer.** If a sentence needs a second read, rewrite it.
- **Check every claim.** Numbers, behaviour and limits must match the code or a
  result you measured. Read the code or run the command; do not guess.
- **Check every link.** It must work and go where the text says.
- **Look at UI text in a browser**, next to real data. A sentence that reads well
  in source can be wrong on the page; for example, a note claiming the hospitals
  differ in size when all three have 400 patients.
- **Keep the tests' hooks.** The healthcare smoke test finds controls by their
  labels ("Requester verified", "Cohort size") and buttons by name ("Ask the
  guard", "Run the pilot"). Rename one only together with `smoke.mjs`.
