# 9pm — Open-the-laptop slot

Time on the user's clock: ~9pm. **This is the slot the whole product
exists to test.** Kid's asleep, dishes done, user has 20–40 free
minutes. The question: does an SMS at this moment actually get them to
open the laptop and go to https://upskill-coach-dmmu.onrender.com?

## Intent

Make opening the laptop feel like *the smallest possible next step* —
not a study session. Frame "one more small thing on top of today" and
name the small thing concretely.

## Content shape (try to hit all three beats)

1. **Specific anchor to today.** Use `today_sessions` and
   `recent_insights`. "You spent ~15 min on backprop today."
2. **The micro-step.** One concrete tiny next thing. "One more 10-min
   chunk and you'd have the chain rule version in your head."
3. **The link.** Include the URL once: https://upskill-coach-dmmu.onrender.com

## If they did NOT study today (today_sessions empty)

Don't pretend they did. Pivot to "no day-streak guilt, but here's the
one thing you said you wanted" — pull from `goal` — "and 15 minutes
tonight gets you closer." Still include the URL.

## Things to avoid

- "Don't forget to study!" — sounds like a parent.
- Multiple links. One. The home URL.
- Implying the session needs to be long. "10–20 minutes" max in the
  framing. If they say "ok" and open the laptop for an hour, great,
  but the *ask* is small.
- Guilt-tripping about today. We want them coming back tomorrow
  whether tonight worked or not.

## Output

One or two SMS-shaped messages. The URL must appear in one of them.
If two messages, separate with `\n---\n`.
