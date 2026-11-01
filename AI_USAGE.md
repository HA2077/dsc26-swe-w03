# AI usage

Keep all three headings exactly as they are. If this file is missing or any heading is
missing, the grader stops and gives 0 ("AI_USAGE.md missing or incomplete"). The content
is not scored, but your Track Mentor reads it and the W8 defence can ask about it.

## Tools

opencode (DeepSeek model) for reading the task docs and starter code, writing `app/models.py`,
`migrations/versions/0002_transfers.py` and `app/main.py` and drafting the throwaway
verification scripts I ran from the shell.

## What AI got wrong

- The ledger check it wrote built the expected balances from `Account` objects after their
  session had closed, so it crashed with `DetachedInstanceError` instead of checking anything.

  I caught it when the script failed and made it read the balances inside an open `Session`.

- The same script replayed a stored idempotency key with a different customer's token and got a
  404, because records are scoped per customer. Replaying with the original token fixed it; the failing assertion is what exposed it.

## In my own words

1. Why does a transfer to someone else's account return 404 and not 403?

Because the transfer belongs to the creator who initiated it. So, if someone else tries to access it with 403 (Forbidden) it would reveal that it is there. On the other end, 404 (Not found) hides whether the transfer exists or not.

2. What would happen to a customer if your idempotency record were written in a separate transaction after the transfer?

It would possibly result in a race condition where the transfer already happened but there is no idempotency record yet. So, if the user retried the transfer it would make a duplicate transfer. And that's what we are trying to avoid using idempotency keys. Overall, the transfer and the idempotency record must be atomic.