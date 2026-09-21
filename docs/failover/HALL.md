# HALL: if the server stops

**Use this sheet when** the Hall server (`hall.local`) has stopped: the Thobe Return and Lunch desks say "One moment…" and then "Use backup — call Admin", and it does not come back after one minute.

**What will happen:** the STANDBY laptop (the spare laptop marked STANDBY) becomes the server in **under 2 minutes**. Nothing that was confirmed is lost: the standby holds a copy that is only a few seconds behind.

**Who does it:** the Admin or the Event IT lead. Desk operators do not do this.

## Steps

1. **Tell the desks to carry on on paper.** Write the student's name and the time on the printed list. Take thobes back and serve lunch as normal. Everything is typed in later.
2. **Switch the old server OFF.** Hold its power button until the light goes out, then unplug its network cable. Do not skip this: if the old server is still running, two servers would each keep half the record.
3. **Go to the STANDBY laptop.** Open the shortcut called **Failover** (a black window opens).
4. **Type this and press Enter:** `./scripts/failover.sh --venue hall`
5. **Watch the window.** It shows "Step 1 of 5" up to "Step 5 of 5". If it stops at step 1 with **STILL ANSWERS**, the old server is not really off: go back to step 2.
6. **Wait for the word DONE.** It also tells you how many seconds it took. If it says anything else, go to "If it does not work" below.
7. **Point the name at the new server.** If the router is set up for it, this is automatic. If not, do what step 4 of the window tells you (it prints the exact instruction).
8. **Check.** Open the Admin page on this laptop. The Sync light should turn 🟢 or 🔵 within a few minutes.
9. **Desks come back on their own** in about a minute. If a screen shows the sign-in page, sign in again.
10. **Type in the paper entries** from the printed list, using the PRN search at each desk. A student whose thobe was not returned goes to the Admin ("Return Waived / Lost").
11. **Do not switch the old server back on** until the IT lead has looked at it. It must be wiped and set up again as the new standby.

## If it does not work

- Ring the **Event IT lead: ____________________**   Second person: ____________________
- Keep working on paper. Nothing is lost while you wait.
- The IT lead can also bring the Hall back from the last backup on the backup drive (taken every 5 minutes).

## Later, after the event

Wait for the Sync light to be 🟢 on all three places. Then the Admin exports the final reports.
