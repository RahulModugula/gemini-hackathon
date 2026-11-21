import Foundation

/// Bundled demo response (3 cards) used by the Mock toggle in Settings.
enum SampleData {
    static let json = #"""
{"claims":[
 {"claim":"The corridor wall is 8 ft tall","kind":"dimension","t_start_s":2.1,"t_end_s":5.4,"quote_or_observation":"\"this wall runs eight feet up\""},
 {"claim":"Door D12 is a 3'-0\" fire-rated door","kind":"spec","t_start_s":7.0,"t_end_s":10.2,"quote_or_observation":"Label on door reads 20 min"},
 {"claim":"Column grid B-4 is where the beam lands","kind":"location","t_start_s":12.5,"t_end_s":15.0,"quote_or_observation":"\"beam lands on B-4\""}],
 "cards":[
 {"id":"c1","claim":"The corridor wall is 8 ft tall","verdict":"conflict","confidence":0.86,"reason":"Sheet A201 section shows the corridor partition at 9'-4\" to underside of deck.","t_start_s":2.1,
  "evidence":[{"sheet":"A201","revision":"R3","crop":"/crops/sample1.png","box_2d_on_crop":[200,150,700,820],"label":"Partition height 9'-4\""}],
  "checks":[{"by":"gemini-hydrogen","verdict":"conflict","note":"Dimension string reads 9'-4\""},{"by":"code","check":"height_ft >= 8","pass":false,"note":"9.33 vs 8"}]},
 {"id":"c2","claim":"Door D12 is a 3'-0\" fire-rated door","verdict":"consistent","confidence":0.92,"reason":"Door schedule lists D12 as 3'-0\" x 7'-0\", 20 min rated.","t_start_s":7.0,
  "evidence":[{"sheet":"A601","revision":"R3","crop":"/crops/sample2.png","box_2d_on_crop":[100,80,350,900],"label":"D12 schedule row"}],
  "checks":[{"by":"gemini-hydrogen","verdict":"consistent","note":"Row matches"},{"by":"code","check":"width == 36in","pass":true}]},
 {"id":"c3","claim":"Column grid B-4 is where the beam lands","verdict":"unclear","confidence":0.4,"reason":"Framing plan is partially illegible at the referenced bay.","t_start_s":12.5,
  "evidence":[],"checks":[{"by":"gemini-hydrogen","verdict":"unclear","note":"Bay not legible"}]}],
 "video_s":16.0}
"""#
}
