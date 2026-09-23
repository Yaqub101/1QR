# Hardware, network and power checklist (one section per place)

For **TODO Phase 18** (Exit Gate 18: "each location passes the hardware checklist"). It follows SYSTEM_SPEC sections 10 (internet failover), 18 (high availability) and 19 (power).

**Who this is for:** a volunteer with a pen. You do not need to understand the computers. You need to look, plug, unplug, switch and tick.

## How to use it

1. **Print only your place's section** (College, Stadium, Hall, or Central). Every section stands on its own, so a few lines repeat on purpose.
2. Tick a box (`[x]`) **only after you have seen it work yourself.** "Somebody said it works" is not a tick.
3. Where you see `____`, write the answer (a number, a time, a name).
4. **[IT lead]** means only the Event IT lead does that step. Watch it done, then tick.
5. If a box will not tick, write what you saw next to it and tell the Event IT lead **the same day**. Do not wait for event day.
6. At the end of your section, sign it. A place has **passed** only when every box is ticked and both signatures are there.

**Words used**

| Word | Means |
|---|---|
| **SERVER** | The laptop that holds this place's records. All the desks talk to it. Label it SERVER. |
| **STANDBY** | A second laptop with a live copy of the records. If the SERVER dies it takes over. Label it STANDBY. |
| **Desk laptop** | A laptop at one desk (one station). Its label is the station name, e.g. `REG-01`. |
| **Scanner** | The USB gun that reads the QR code. It types the code and presses Enter, like a keyboard. |
| **UPS** | The battery box that keeps things running when the mains power cuts. |
| **Uplinks** | The two ways to reach the Internet: the venue broadband, and the 4G/5G SIM router. |

**Numbers.** The desk counts below are the **starting plan** from TODO Phase 0 (Registration 6; Robe Allocation 5; Seating 4; Queue 3; Stage 1 plus 1 backup; Robe Return 4; Lunch 5). The project owner has not confirmed them. Write the final number in each blank when it is decided.

**Why so many spares:** the whole design says one broken thing must never stop a place (SYSTEM_SPEC section 18). Spares are how that is kept.

---

# COLLEGE (Registration)

Place: ______________________  Date checked: ____ / ____ / ______

## A. Network

- [ ] **Two Internet connections are plugged into the main router**: venue broadband **and** the 4G/5G SIM router. Both show a "connected" light.
- [ ] **A spare router** is in its box, labelled SPARE ROUTER, and has been switched on once to prove it works. **[IT lead]** It is set up exactly like the main one.
- [ ] **The SERVER and the STANDBY are joined to the router with Ethernet cables** (not Wi-Fi).
- [ ] **Two network switches** are set up (or one switch plus a spare in a box).
- [ ] **Fixed addresses are set in the router** for the SERVER and the STANDBY. **[IT lead]** Write them here: SERVER ____________________  STANDBY ____________________
- [ ] **The desks reach the server by one fixed name**, planned as `college.local` (written on the setup card). **[IT lead]** Write the name in use: ____________________. *(Note for the IT lead: docs/HA.md advises a router DNS name over `.local` names; decide, and write the final name on every failover sheet.)*
- [ ] **Desk laptops are on this place's own network.** They have no other Internet source (no phone hotspot on a desk laptop).
- [ ] **UNPLUG TEST.** Unplug the broadband cable from the router. On a desk laptop, keep the station page open and confirm the desks keep working (scan a test QR). Nobody touches any setting. Plug it back in.
  - Desks kept working with nothing done by anyone: [ ]
  - Seconds until the SERVER's Admin page still showed a live connection (or note "kept working"): ______

## B. Computers

- [ ] **SERVER laptop** is labelled SERVER, on Ethernet, and its health page shows `"db": "up"` (open `http://college.local:8000/health`, or the address on the setup card).
- [ ] **STANDBY laptop** is labelled STANDBY, on Ethernet, and switched on. **[IT lead]** It is copying from the SERVER.
- [ ] **Desk laptops:** number needed ____ (plan: 6 Registration). Each has its station name on a label. Each has been **bound to its station** by the Admin ("Set up this laptop"). Each has been signed in once by its own operator.
- [ ] **One spare desk laptop** is set up, charged and labelled SPARE.
- [ ] **REBIND TEST.** The Admin binds the spare laptop to any station. It takes **under a minute**. Time it: ______ seconds.
- [ ] **Chargers and batteries.** Every laptop is fully charged and has its charger. Desks are near a power socket.
- [ ] **Sleep is OFF while plugged in** on the SERVER and the STANDBY. (The screen may switch off. The computer must not go to sleep.)
- [ ] **The servers start by themselves** after a restart. **[IT lead]** Docker is set to start with the machine.
- [ ] **Disk encryption is ON** on the SERVER, the STANDBY, the spare and every desk laptop (they hold student data). **[IT lead]** The recovery keys are kept by the IT lead, not on the laptops.
- [ ] **Clock is correct** on the SERVER and the STANDBY: right date, right time, right time zone. Checked against a phone or a trusted clock. Time shown: ________  Real time: ________

## C. Scanners

- [ ] Scanners needed ____ (one per desk) **plus at least 1 spare** (College has one kind of desk, so 1 spare). Total ready: ____
- [ ] **SCANNER TEST, every scanner.** Open a text box (Notepad or the scan box on a station screen). Scan any QR code. The text appears **and the cursor jumps to a new line.** That is how you know it "types the code and presses Enter".
  - Scanner numbers tested and passed (write the label numbers): ______________________________
- [ ] **Every kind of laptop has been tried with a scanner.** If the desk laptops are different models, each model has been tested. Models tested: ______________________________
- [ ] Spare scanners are labelled SPARE and tested the same way.

## D. Power

- [ ] **UPS is fitted** and everything below is plugged into it: SERVER, STANDBY, router(s), switch(es), the 4G/5G router (and the scanner USB hub, if it has its own power plug).
- [ ] **UPS TEST with the real load.** Everything above is running. Switch off the mains (at the wall, or pull the UPS plug) and start a stopwatch. The SERVER and the network stay on. Aim for **30 minutes or more.**
  - Minutes it lasted: ______  (Pass = 30 or more)
  - After the test, the UPS is plugged back in and is fully charged again: [ ]
- [ ] **Surge-protected extension boards** are in place, **plus one spare board**.
- [ ] Desk laptops are on their own batteries and mains, **not** on the UPS.
- [ ] *(If the venue has a generator or backup power)* The venue told us the changeover time: ______ seconds. It is shorter than the UPS runtime.

## E. Paper (the last resort)

- [ ] **Fallback sheets printed** after the master list was frozen. **[IT lead]** They were made with `scripts/fallback_sheets.py`. The **total on the sheet equals the master count** the Admin gave us: sheet says ______, Admin says ______.
- [ ] The Registration sheet is at **every** Registration desk, plus a spare copy.
- [ ] The **one-page failover sheet** for College ([docs/failover/COLLEGE.md](../failover/COLLEGE.md)) is printed and kept **next to the STANDBY laptop** and with the Admin. The phone numbers on it are filled in.
- [ ] The **one-page instruction sheet** for the Registration desk is at every desk.
- [ ] Pens and clipboards for the paper sheets.

**Signed off**

Checked by (volunteer): ______________________  Date/time: ______________
Verified by (Event IT lead): ______________________  Date/time: ______________
Passed:  YES / NO   Problems still open: ________________________________________

---

# STADIUM (Robe Allocation, Seating, Queue, Stage, and the big screen)

Place: ______________________  Date checked: ____ / ____ / ______

## A. Network

- [ ] **Two Internet connections are plugged into the main router**: venue broadband **and** the 4G/5G SIM router. Both show a "connected" light.
- [ ] **A spare router** is in its box, labelled SPARE ROUTER, and has been switched on once to prove it works. **[IT lead]** It is set up exactly like the main one.
- [ ] **The SERVER, the STANDBY and both Stage laptops are joined with Ethernet cables where possible** (SERVER and STANDBY must be on Ethernet).
- [ ] **Two network switches** are set up (or one switch plus a spare in a box).
- [ ] **Fixed addresses are set in the router** for the SERVER and the STANDBY. **[IT lead]** Write them here: SERVER ____________________  STANDBY ____________________
- [ ] **The desks reach the server by one fixed name**, planned as `stadium.local` (written on the setup card). **[IT lead]** Write the name in use: ____________________. *(Note for the IT lead: docs/HA.md advises a router DNS name over `.local` names; decide, and write the final name on every failover sheet.)*
- [ ] **Desk laptops are on this place's own network.** They have no other Internet source.
- [ ] **UNPLUG TEST.** Unplug the broadband cable from the router. On a desk laptop, keep the station page open and confirm the desks keep working (scan a test QR). Nobody touches any setting. Plug it back in.
  - Desks kept working with nothing done by anyone: [ ]
  - Seconds until the SERVER's Admin page still showed a live connection (or note "kept working"): ______
- [ ] **SWITCH/ROUTER SPARE TEST.** The IT lead swaps in the spare switch and the spare router. The desks come back. Time to recover: ______ minutes.

## B. Computers

- [ ] **SERVER laptop** is labelled SERVER, on Ethernet, and its health page shows `"db": "up"` (open `http://stadium.local:8000/health`, or the address on the setup card).
- [ ] **STAGE laptop** and **STAGE BACKUP laptop** are both set up. **The STAGE BACKUP laptop is also the STANDBY** for the Stadium (SYSTEM_SPEC section 18). Label it STAGE BACKUP / STANDBY. **[IT lead]** It is copying from the SERVER.
- [ ] **Desk laptops:** number needed ____ (plan: Robe Allocation 5, Seating 4, Queue 3 = 12). Each has its station name on a label. Each has been **bound to its station** by the Admin. Each has been signed in once by its own operator.
- [ ] **One spare desk laptop** is set up, charged and labelled SPARE.
- [ ] **REBIND TEST.** The Admin binds the spare laptop to any station. It takes **under a minute**. Time it: ______ seconds.
- [ ] **Chargers and batteries.** Every laptop is fully charged and has its charger. Desks are near a power socket.
- [ ] **Sleep is OFF while plugged in** on the SERVER, the STAGE laptop and the STAGE BACKUP laptop.
- [ ] **The servers start by themselves** after a restart. **[IT lead]** Docker is set to start with the machine.
- [ ] **Disk encryption is ON** on the SERVER, both Stage laptops, the spare and every desk laptop (they hold student data). **[IT lead]** The recovery keys are kept by the IT lead, not on the laptops.
- [ ] **Clock is correct** on the SERVER and the STAGE BACKUP/STANDBY: right date, right time, right time zone. Time shown: ________  Real time: ________

## C. The big screen (LED) and the stage

- [ ] **The LED controller and BOTH Stage laptops are on their own UPS**, separate from the SERVER's UPS.
- [ ] **A spare HDMI cable and the adapters** are in the stage bag. Both were tried with the real screen.
- [ ] **The big screen shows the university holding screen** (the event name and its text) when nobody is on stage. **[IT lead]** If it shows only "Convocation Ceremony" with nothing under it, tell the IT lead: the event name and holding text are set in the freeze checklist.
- [ ] **STAGE laptop test.** The Stage operator signs in on the STAGE laptop and can see CURRENT / NEXT / AFTER NEXT. **[IT lead]** Only one laptop runs the stage at a time. The other laptop's TAKE OVER button works.
- [ ] **HDMI SWAP TEST.** Pull the HDMI cable and put the spare in. The screen comes back. Seconds: ______
- [ ] **Generator / mains changeover.** The venue told us how long the power is off when it switches between mains and generator: ______ seconds. **The UPS is rated to cover it.** Name of the venue person who told us: ______________________

## D. Scanners

- [ ] Scanners needed ____ (one per scanning desk: Robe Allocation, Seating, Queue) **plus at least 3 spares** (one for each of the three kinds of desk). The Stage desk does not scan. Total ready: ____
- [ ] **SCANNER TEST, every scanner.** Open a text box (Notepad or the scan box on a station screen). Scan any QR code. The text appears **and the cursor jumps to a new line.**
  - Scanner numbers tested and passed: ______________________________
- [ ] **Every kind of laptop has been tried with a scanner** (each model). Models tested: ______________________________
- [ ] Spare scanners are labelled SPARE and tested the same way.

## E. Power

- [ ] **UPS 1 (network and server)** is fitted and these are plugged into it: SERVER, router(s), switch(es), the 4G/5G router (and the scanner USB hub, if it has its own power plug).
- [ ] **UPS 2 (stage, separate)** is fitted and these are plugged into it: the LED controller, the STAGE laptop, the STAGE BACKUP/STANDBY laptop.
- [ ] **UPS TEST with the real load, both UPS units.** Everything above is running. Switch off the mains and start a stopwatch. Aim for **30 minutes or more.**
  - UPS 1 minutes: ______   UPS 2 minutes: ______  (Pass = 30 or more each)
  - Both UPS units are plugged back in and fully charged again: [ ]
- [ ] **Surge-protected extension boards** are in place, **plus one spare board**.
- [ ] Desk laptops are on their own batteries and mains, **not** on the UPS.

## F. Paper (the last resort)

- [ ] **Fallback sheets printed** after the master list was frozen. **[IT lead]** They were made with `scripts/fallback_sheets.py`. The **total on every sheet equals the master count**: sheet says ______, Admin says ______.
- [ ] The right sheet is at **every** desk: Robe Allocation, Seating, Queue and Stage. Plus a spare copy of each.
- [ ] The **one-page failover sheet** for the Stadium ([docs/failover/STADIUM.md](../failover/STADIUM.md)) is printed and kept **next to the STAGE BACKUP/STANDBY laptop** and with the Admin. The phone numbers on it are filled in.
- [ ] The **one-page instruction sheet** for each desk is at every desk (and one for the Stage operator).
- [ ] Pens and clipboards for the paper sheets.

**Signed off**

Checked by (volunteer): ______________________  Date/time: ______________
Verified by (Event IT lead): ______________________  Date/time: ______________
Passed:  YES / NO   Problems still open: ________________________________________

---

# HALL (Robe Return and Lunch)

Place: ______________________  Date checked: ____ / ____ / ______

## A. Network

- [ ] **Two Internet connections are plugged into the main router**: venue broadband **and** the 4G/5G SIM router. Both show a "connected" light.
- [ ] **A spare router** is in its box, labelled SPARE ROUTER, and has been switched on once to prove it works. **[IT lead]** It is set up exactly like the main one.
- [ ] **The SERVER and the STANDBY are joined to the router with Ethernet cables** (not Wi-Fi).
- [ ] **Two network switches** are set up (or one switch plus a spare in a box).
- [ ] **Fixed addresses are set in the router** for the SERVER and the STANDBY. **[IT lead]** Write them here: SERVER ____________________  STANDBY ____________________
- [ ] **The desks reach the server by one fixed name**, planned as `hall.local` (written on the setup card). **[IT lead]** Write the name in use: ____________________. *(Note for the IT lead: docs/HA.md advises a router DNS name over `.local` names; decide, and write the final name on every failover sheet.)*
- [ ] **Desk laptops are on this place's own network.** They have no other Internet source.
- [ ] **UNPLUG TEST.** Unplug the broadband cable from the router. On a desk laptop, keep the station page open and confirm the desks keep working (scan a test QR). Nobody touches any setting. Plug it back in.
  - Desks kept working with nothing done by anyone: [ ]
  - Seconds until the SERVER's Admin page still showed a live connection (or note "kept working"): ______

## B. Computers

- [ ] **SERVER laptop** is labelled SERVER, on Ethernet, and its health page shows `"db": "up"` (open `http://hall.local:8000/health`, or the address on the setup card).
- [ ] **STANDBY laptop** is labelled STANDBY, on Ethernet, and switched on. **[IT lead]** It is copying from the SERVER.
- [ ] **Desk laptops:** number needed ____ (plan: Robe Return 4, Lunch 5 = 9). Each has its station name on a label. Each has been **bound to its station** by the Admin. Each has been signed in once by its own operator.
- [ ] **One spare desk laptop** is set up, charged and labelled SPARE.
- [ ] **REBIND TEST.** The Admin binds the spare laptop to any station. It takes **under a minute**. Time it: ______ seconds.
- [ ] **Chargers and batteries.** Every laptop is fully charged and has its charger. Desks are near a power socket.
- [ ] **Sleep is OFF while plugged in** on the SERVER and the STANDBY.
- [ ] **The servers start by themselves** after a restart. **[IT lead]** Docker is set to start with the machine.
- [ ] **Disk encryption is ON** on the SERVER, the STANDBY, the spare and every desk laptop (they hold student data). **[IT lead]** The recovery keys are kept by the IT lead, not on the laptops.
- [ ] **Clock is correct** on the SERVER and the STANDBY: right date, right time, right time zone. Time shown: ________  Real time: ________

## C. Scanners

- [ ] Scanners needed ____ (one per desk) **plus at least 2 spares** (one for Robe Return, one for Lunch). Total ready: ____
- [ ] **SCANNER TEST, every scanner.** Open a text box (Notepad or the scan box on a station screen). Scan any QR code. The text appears **and the cursor jumps to a new line.**
  - Scanner numbers tested and passed: ______________________________
- [ ] **Every kind of laptop has been tried with a scanner** (each model). Models tested: ______________________________
- [ ] Spare scanners are labelled SPARE and tested the same way.

## D. Power

- [ ] **UPS is fitted** and everything below is plugged into it: SERVER, STANDBY, router(s), switch(es), the 4G/5G router (and the scanner USB hub, if it has its own power plug).
- [ ] **UPS TEST with the real load.** Everything above is running. Switch off the mains and start a stopwatch. The SERVER and the network stay on. Aim for **30 minutes or more.**
  - Minutes it lasted: ______  (Pass = 30 or more)
  - After the test, the UPS is plugged back in and is fully charged again: [ ]
- [ ] **Surge-protected extension boards** are in place, **plus one spare board**.
- [ ] Desk laptops are on their own batteries and mains, **not** on the UPS.
- [ ] *(If the venue has a generator or backup power)* The venue told us the changeover time: ______ seconds. It is shorter than the UPS runtime.

## E. Paper (the last resort)

- [ ] **Fallback sheets printed** after the master list was frozen. **[IT lead]** They were made with `scripts/fallback_sheets.py`. The **total on every sheet equals the master count**: sheet says ______, Admin says ______.
- [ ] The Robe Return sheet is at **every** Robe Return desk and the Lunch sheet at **every** Lunch desk, plus a spare copy of each.
- [ ] The **one-page failover sheet** for the Hall ([docs/failover/HALL.md](../failover/HALL.md)) is printed and kept **next to the STANDBY laptop** and with the Admin. The phone numbers on it are filled in.
- [ ] The **one-page instruction sheet** for each desk is at every desk.
- [ ] Pens and clipboards for the paper sheets.

**Signed off**

Checked by (volunteer): ______________________  Date/time: ______________
Verified by (Event IT lead): ______________________  Date/time: ______________
Passed:  YES / NO   Problems still open: ________________________________________

---

# CENTRAL (the cloud server and the Admin's kit)

Central is not a room with desks. It is a cloud computer that keeps the merged copy of everything. **The event does not wait for it** (SYSTEM_SPEC section 7): if it is down, the three places keep working and catch up later. It still has to be ready, because the Admin's whole-event view and the off-site copy depend on it.

Date checked: ____ / ____ / ______

## A. The cloud computer (all **[IT lead]**; the volunteer watches and ticks)

- [ ] **The university owns the cloud account** (SYSTEM_SPEC section 25, point 10). The build team has been given deploy access. Account owner's name: ______________________
- [ ] **The computer is in a data centre in India** (recommended for data residency). Provider and region: ______________________
- [ ] **The central system is running** and its health page answers.
- [ ] **Daily snapshots are switched on.** Time of the last snapshot: ______________
- [ ] **The connection is secure (https, with a proper certificate).** A browser shows no warning.
- [ ] **Each place has its own key.** Three keys were made at central (one each for College, Stadium, Hall). Each key was put into **that** place's settings and nowhere else. The keys are **not** written on this sheet.
- [ ] **Each place can reach central** through **both** uplinks. On each place's Admin dashboard, the sync light shows 🟢 with the broadband on, and again with the broadband unplugged (4G/5G).
- [ ] **Central's backup goes to a separate disk** (a full copy every day).
- [ ] **The Admin and the Deputy Admin accounts exist on central**, each with their own login. (Central accounts are made separately from the three places.)
- [ ] **The clock is correct.**

## B. The Admin's kit

- [ ] **The Admin's laptop and the Deputy's laptop are encrypted**, charged, and can open the central dashboard **and** each place's Admin page.
- [ ] **A USB drive is labelled MILESTONE BACKUPS** and held by the Admin (SYSTEM_SPEC section 21). It is encrypted. It has room for three backups from each place.
- [ ] **A phone list is printed** with: Admin, Deputy Admin, Event IT lead and a second technical person, and the university IT contact. It is with the Admin and at each place's STANDBY laptop.
- [ ] **Off-site copy.** We know where the final backup goes after the event (one copy is not in any of the three places): ______________________

**Signed off**

Checked by (volunteer): ______________________  Date/time: ______________
Verified by (Event IT lead): ______________________  Date/time: ______________
Passed:  YES / NO   Problems still open: ________________________________________

---

## After all four are signed

The project owner reads the four signed sections and, if every place passed, ticks **Exit Gate 18** in [docs/TODO.md](../TODO.md). Untested items (the broadband unplug test, the UPS runtime, the scanner on every laptop model, the rebind time) can only be proved on the real equipment; these boxes are that proof.
