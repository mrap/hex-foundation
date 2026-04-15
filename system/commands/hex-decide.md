# /hex-decide — Structured Decision

Use the decision template to think through a choice systematically.

## Usage

The user says `/hex-decide <topic>` or just `/hex-decide` and describes the decision.

## Steps

1. **Create decision file:** Copy `.hex/templates/decision-template.md` to `me/decisions/{topic}-YYYY-MM-DD.md` (use `date +%Y-%m-%d` for the date).

2. **Fill in context:** Ask the user: "What's the decision about? What triggered it?"

3. **Enumerate options:** Ask: "What are the options you're considering?" List them with pros and cons.

4. **Adversarial check:** For each option, identify the weakest assumption. What could go wrong?

5. **Record the decision:** Once the user decides, fill in the Decision, Reasoning, and Impact sections.

6. **Report:** "Decision recorded at `me/decisions/{topic}-YYYY-MM-DD.md`."
