# Label audit: heldout_v3 (2026-11-21)

Method: hand-read, no model calls. Every label was derived from the PDF words in `build/words/<sheet>.json` (exact text line, schedule row or single-word evidence) and confirmed on a pymupdf crop of the claim's bbox. 51 claims, 20 conflict, 18 consistent, 13 unclear.
Unclear = the asked-for value (usually a maker or name) occurs on no current sheet; checked case-insensitively against the word text of all 54 current sheets (`eval/test_label_sets.py::test_unclear_tokens_absent_from_every_current_sheet`).
Overlap guard: ids and claim texts are disjoint from dev_v3, heldout_v1 and heldout_v2 (checked in the test). Targets were chosen on sheets and notes not used by those sets (S900/S200 structural notes, M000 notes, ME100 hood and coil rows, M501 details, A1000 wall notes, A1001 glazing and frame notes, AD201 general demolition notes, E100 exterior-lighting note, P102 kitchen schedule, L1.00, C1.20). A few sit on sheets that held-out v2 also touches (AD201, E100) but on different notes.

| id | label | sheet | evidence |
|---|---|---|---|
| ho3-01 | consistent | S900 (dwg-p029) | read on dwg-p029 (S900): Reinforced concrete inspection note: OWNER SHALL ENGAGE AN INSPECTION SERVICE for all reinforcing bar inspections. |
| ho3-02 | conflict | S900 (dwg-p029) | read on dwg-p029 (S900): Notice period is AT LEAST 7 DAYS PRIOR TO CONCRETE POUR, not 3. |
| ho3-03 | consistent | S900 (dwg-p029) | read on dwg-p029 (S900): Masonry note: ONLY 2-CELL BLOCK IS ALLOWED FOR REINFORCED MASONRY CONSTRUCTION. |
| ho3-04 | conflict | S900 (dwg-p029) | read on dwg-p029 (S900): Cleanout trigger is a pour height exceeding 5 FEET, not 8. |
| ho3-05 | conflict | S900 (dwg-p029) | read on dwg-p029 (S900): Test-cylinder note requires a minimum 3000 PSI before backfill, not 2000. |
| ho3-06 | consistent | S900 (dwg-p029) | read on dwg-p029 (S900): Slab tolerance note: overall FF 35 and FL 25 (min local 20 and 15). |
| ho3-07 | conflict | S900 (dwg-p029) | read on dwg-p029 (S900): Bond beam lintels BEAR A MINIMUM OF 8" AT EACH END. |
| ho3-08 | unclear | S900 (dwg-p029) | Joint reinforcement note says only 'such as DUR-O-WALL'; no Hohmann & Barnard on any sheet. Token ['Hohmann'] occurs on no current sheet (checked all current sheets, case-insensitive substring). |
| ho3-09 | unclear | S900 (dwg-p029) | Concrete notes cover rebar inspection and shop drawings but name no steel supplier; Nucor on no sheet. Token ['Nucor'] occurs on no current sheet (checked all current sheets, case-insensitive substring). |
| ho3-10 | consistent | S200 (dwg-p027) | read on dwg-p027 (S200): S200 keyed note: 8" CAST-IN-PLACE CEILING SLAB BEARING ON CMU WALLS. |
| ho3-11 | conflict | S200 (dwg-p027) | read on dwg-p027 (S200): New CMU wall note: (2) #5 @ 24" o/c for the full height. |
| ho3-12 | consistent | S200 (dwg-p027) | read on dwg-p027 (S200): Keyed note 4: ALL MASONRY OPENINGS SHALL NOT EXCEED 4'-0" WIDTH. |
| ho3-13 | consistent | S200 (dwg-p027) | read on dwg-p027 (S200): Plan note: new 5" slab inside shelter, new 4" slab outside to match existing. |
| ho3-14 | unclear | S200 (dwg-p027) | S200 keyed notes give door openings 'for concept only' and name no door or frame maker; Ceco on no sheet. Token ['Ceco'] occurs on no current sheet (checked all current sheets, case-insensitive substring). |
| ho3-15 | consistent | M000 (dwg-p036) | read on dwg-p036 (M000): M000 general note 6: curb a MINIMUM OF 8" ABOVE THE INSULATION. |
| ho3-16 | conflict | M000 (dwg-p036) | read on dwg-p036 (M000): M000 note 13.B: PLUS OR MINUS 5 PERCENT. |
| ho3-17 | consistent | M000 (dwg-p036) | read on dwg-p036 (M000): M000 note 16: NOT MORE THAN 48 INCHES AFF OR LESS THAN 40 INCHES AFF. |
| ho3-18 | conflict | M000 (dwg-p036) | read on dwg-p036 (M000): M000 note 17: air gap NOT LESS THAN TWO TIMES the effective opening. |
| ho3-19 | conflict | M000 (dwg-p036) | read on dwg-p036 (M000): M000 note 20: NEW G90 HOT-DIPPED GALVANIZED SHEET. |
| ho3-20 | conflict | M000 (dwg-p036) | read on dwg-p036 (M000): Narrative: A NEW HEATING ONLY MAKE-UP AIR UNIT. |
| ho3-21 | consistent | M000 (dwg-p036) | read on dwg-p036 (M000): Narrative: TWO UNITS WILL BE REMOVED AND RELOCATED; CLOTHES DRYER VENT TO TERMINATE THROUGH ROOF. |
| ho3-22 | unclear | M000 (dwg-p036) | Note 20 says only SMACNA gauge and reinforcement; no connector maker, Ductmate on no sheet. Token ['Ductmate'] occurs on no current sheet (checked all current sheets, case-insensitive substring). |
| ho3-23 | consistent | ME100 (dwg-p043) | read on dwg-p043 (ME100): Hood schedule row H-6: RELIEF, F-6, GREENHECK, FGR-24X24, 1850 CFM. |
| ho3-24 | conflict | ME100 (dwg-p043) | read on dwg-p043 (ME100): Hood schedule row H-10 is 1850 CFM. |
| ho3-25 | consistent | ME100 (dwg-p043) | read on dwg-p043 (ME100): DX coil schedule row CC-3: (E) F-3, TRANE, 4TTR7024A, 800 CFM. |
| ho3-26 | conflict | ME100 (dwg-p043) | read on dwg-p043 (ME100): DX coil schedule row CC-6 has ROWS = 4. |
| ho3-27 | conflict | ME100 (dwg-p043) | read on dwg-p043 (ME100): Row CC-7 manufacturer is TRANE. |
| ho3-28 | conflict | ME100 (dwg-p043) | read on dwg-p043 (ME100): H-10 comments column is note 2: 24" CURB, MOTORIZED DAMPER, INSECT SCREEN. |
| ho3-29 | unclear | ME100 (dwg-p043) | All hood rows are Greenheck; Loren Cook on no sheet. Token ['Loren Cook'] occurs on no current sheet (checked all current sheets, case-insensitive substring). |
| ho3-30 | unclear | ME100 (dwg-p043) | Coil schedule lists only Trane; condenser matches not given; Lennox on no sheet. Token ['Lennox'] occurs on no current sheet (checked all current sheets, case-insensitive substring). |
| ho3-31 | consistent | M501 (dwg-p042) | read on dwg-p042 (M501): M501 grease exhaust fan detail: WELDED DUCT MIN. OF 18'' ABOVE ROOF DECK. |
| ho3-32 | conflict | M501 (dwg-p042) | read on dwg-p042 (M501): Furnace detail: 12"+-1" above air inlet. |
| ho3-33 | unclear | M501 (dwg-p042) | Detail says UL LISTED FAN & CURB ASSEMBLY, SEE SCHEDULE FOR FAN TYPE; Captiveaire on no sheet. Token ['Captiveaire'] occurs on no current sheet (checked all current sheets, case-insensitive substring). |
| ho3-34 | consistent | A1000 (add1-p014) | read on add1-p014 (A1000): A1000 note: untagged gyp board & metal stud wall assumed TYPE "A1". |
| ho3-35 | conflict | A1000 (add1-p014) | read on add1-p014 (A1000): Note C1: batts at all BATHROOM walls. |
| ho3-36 | conflict | A1000 (add1-p014) | read on add1-p014 (A1000): Legend: CORRIDOR PARTITION (SP) PRIORITY 14; PRIORITY 13 is hazardous spaces. |
| ho3-37 | unclear | A1000 (add1-p014) | B5 requires UL-listed firestop systems but names no maker; Hilti on no sheet. Token ['Hilti'] occurs on no current sheet (checked all current sheets, case-insensitive substring). |
| ho3-38 | conflict | AD201 (add2-p006) | read on add2-p006 (AD201): Demo note 11: removal of ACM is the OWNER's responsibility. |
| ho3-39 | consistent | AD201 (add2-p006) | read on add2-p006 (AD201): Demo note: removed fixtures remain the property of the owner. |
| ho3-40 | consistent | AD201 (add2-p006) | read on add2-p006 (AD201): Demo scope note. |
| ho3-41 | conflict | AD201 (add2-p006) | read on add2-p006 (AD201): REMOVE definition: removal from owner's site AT THE CONTRACTOR'S EXPENSE. |
| ho3-42 | consistent | AD201 (add2-p006) | read on add2-p006 (AD201): Demo note: GC responsible for interim storage of toilet room accessories, corner guards, bumper guards, fire extinguishers. |
| ho3-43 | unclear | AD201 (add2-p006) | Note 11 mentions ACM but no survey report or firm; Terracon on no sheet. Token ['Terracon'] occurs on no current sheet (checked all current sheets, case-insensitive substring). |
| ho3-44 | consistent | A1001 (add1-p015) | read on add1-p015 (A1001): A1001 frame note 2. |
| ho3-45 | conflict | A1001 (add1-p015) | read on add1-p015 (A1001): Borrowed lite definition: SHALL NOT INCLUDE MIRRORS IN TOILET ROOMS. |
| ho3-46 | unclear | A1001 (add1-p015) | Glazing notes list GL-1..GL-3 only, no supplier; Oldcastle on no sheet. Token ['Oldcastle'] occurs on no current sheet (checked all current sheets, case-insensitive substring). |
| ho3-47 | consistent | E100 (add2-p010) | read on add2-p010 (E100): E100 exterior lighting note. |
| ho3-48 | conflict | E100 (add2-p010) | read on add2-p010 (E100): E100 note: APPROXIMATELY 1'-6" ABOVE TOP OF DOOR. |
| ho3-49 | unclear | P102 (dwg-p035) | Item 14 lists DISHWASHER, UNDER COUNTER, no maker; Hobart on no sheet. Token ['Hobart'] occurs on no current sheet (checked all current sheets, case-insensitive substring). |
| ho3-50 | unclear | L1.00 (dwg-p010) | L1.00 calls out PERIMETER FENCE (6' HT.) and CHAIN LINK only; no fence maker; Ameristar on no sheet. Token ['Ameristar'] occurs on no current sheet (checked all current sheets, case-insensitive substring). |
| ho3-51 | unclear | C1.20 (dwg-p006) | Grading plan notes the playground bumper grade only; no surfacing product; Fibar on no sheet. Token ['Fibar'] occurs on no current sheet (checked all current sheets, case-insensitive substring). |
