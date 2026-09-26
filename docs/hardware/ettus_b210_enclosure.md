# Ettus USRP B210 in its steel enclosure: mechanical data

For planning the radio's mounting at the station. The numbers come from NI's customer
drawing of the enclosed radio:

<!-- widths: 1.6,5.1 -->
| | |
|---|---|
| Drawing | NI customer drawing "Ettus USRP B210", CAGE code 7U296, dated 2017-01-10, scale 3/4, third-angle projection, dimensions in inches [millimeters] |
| Published by | Ettus Research, Knowledge Base page "B200/B210/B200mini/B205mini", Physical Specifications, Drawings, "B200/B210 Enclosure" |
| Local copy | `Ettus_B210_Enclosure_Drawing.pdf` in this folder, kept in the NAS repository only (the sheet carries NI's copyright notice). Retrieved 2026-09-26 from the Internet Archive's copy of the Knowledge Base (snapshot 2024-07-14), because the live site blocks automated downloads. SHA-256 `1a99e7ab57f01c04303fe055ab32c03200b187fc48fbf017005e26ed33eabfb6` |
| Applies to | Green B210 boards, revision 6 and later, in the Ettus full steel enclosure kit. Older white boards, revisions 1 to 4, shipped in a Takachi aluminum case of a different size: check that the case measures 4.82 in wide before cutting metal |

## Envelope

| Feature | Inches | mm | Note |
|---|---|---|---|
| Length over the SMA connectors | 6.99 | 177.6 | |
| Length of the case body | 6.19 | 157 | Derived: overall length less 0.40 in of connector at each end |
| Width | 4.82 | 122.3 | |
| Height including the feet | 1.47 | 37.3 | |
| Height of the case alone | 1.33 | 33.7 | Feet add about 0.14 in, 3.6 mm (derived) |
| SMA protrusion, each end | 0.40 | 10.1 | |
| Feet, in from each long side | 0.57 | 14.4 | Four adhesive feet on the bottom; 3.68 in, 93.6 mm, apart across the width |
| Feet, back from the front face | 0.85 and 5.57 | 21.5 and 141.6 | 4.72 in, 120 mm, apart along the length |
| Weight | | 676 g | NI USRP-2901 specifications, the same radio and case. NI's page prints the size as 12.5 × 9.4 × 3.8 cm, a typo: its inch figures, 7.0 × 4.9 × 1.5 in, agree with the drawing |

## Connectors

Front panel, measured from the left edge as you face it; all four SMA centers are 0.47 in,
12 mm, above the bottom of the case (not counting the feet):

| Connector | Inches | mm |
|---|---|---|
| RF-A TX/RX | 1.10 | 27.8 |
| RF-A RX2 | 1.90 | 48.2 |
| RF-B RX2 | 2.90 | 73.6 |
| RF-B TX/RX | 3.70 | 93.9 |

Rear panel, measured from the right edge as you face it:

| Connector | Inches across | mm across | Height above the case bottom |
|---|---|---|---|
| GPS antenna, SMA | 3.81 | 96.9 | 0.470 in, 11.94 mm |
| 10 MHz reference, SMA | 3.03 | 77.0 | 0.470 in, 11.94 mm |
| USB 3.0, type B | 2.34 | 59.5 | 0.724 in, 18.39 mm |
| PPS, SMA | 1.58 | 40.3 | 0.470 in, 11.94 mm |
| 6 V power jack | 0.85 | 21.7 | 0.719 in, 18.27 mm |

The labels follow Ettus's panel artwork (USRP B210 plates, Rev 2, 2014-11-15).

## Mounting notes

- The case has no mounting holes or flanges: only the four adhesive feet and a security
  slot at the front and at the rear. Cradle or clamp the case rather than drilling the steel;
  drilling puts metal chips over the board.
- The foot pattern above is a ready-made pattern for rubber standoffs if the mount
  replaces the adhesive feet.
- Leave room at both ends for the SMA cables, the USB 3.0 plug and the power plug,
  including their bend radius; about 50 mm per end is a sensible starting allowance.
- The bare board is 154.7 × 100 mm and 1.6 mm thick, 177.6 mm over the SMAs, 350 g (Ettus
  spec sheet; board drawing `cu_ettus_b210_cca.pdf` at https://files.ettus.com/b2x0_enclosure/).
