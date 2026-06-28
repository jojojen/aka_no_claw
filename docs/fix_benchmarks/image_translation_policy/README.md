# Image Translation Policy Benchmark

Last reviewed: 2026-06-24
Status: Current
Owner area: agent-maintenance

This benchmark supports repair work for image translation quality. It is meant
to catch the failure mode where OCR line breaks are translated literally even
when the image is a menu, sign, UI, label, document, notice, or comic.

The benchmark does not define one canonical translation for every image.
Instead, each case defines the required content type and translation policy in
`cases.json`. The target behavior is:

1. OCR should be faithful and should preserve enough structure for downstream
   processing.
2. The system should classify the image content type before translation.
3. The translation step should choose a policy appropriate to that content type.
4. The final answer should be natural Traditional Chinese while preserving the
   structure and values that matter for the image type.

## Cases

| Case | Fixture | Type | Main Risk |
| --- | --- | --- | --- |
| `menu_yakitori` | `fixtures/menu_yakitori.jpg` | menu photo | Losing item/price rows or turning menu text into prose. |
| `sign_airport_osaka` | `fixtures/sign_airport_osaka.jpg` | wayfinding sign | Losing short sign wording or direction semantics. |
| `nutrition_fda_2014` | `fixtures/nutrition_fda_2014.jpg` | nutrition label | Dropping numbers, units, or table hierarchy. |
| `document_commonsense` | `fixtures/document_commonsense.jpg` | historical document scan | Over-modernizing or ignoring title/list structure. |
| `ui_libreoffice_writer` | `fixtures/ui_libreoffice_writer.png` | software UI screenshot | Summarizing instead of translating UI labels. |
| `comic_dwig_private_secretary` | `fixtures/comic_dwig_private_secretary.jpg` | comic dialogue | Copying OCR line breaks instead of grouping dialogue. |
| `handwritten_japanese_notice` | `fixtures/handwritten_japanese_notice.png` | handwritten public notice | Pretending uncertain handwriting is certain. |

## Safety And Licensing

Fixtures were downloaded from Wikimedia Commons source pages and re-saved locally
after resizing to fit within `1200x1200`. Re-saving strips EXIF/GPS metadata from
the local copies. Source, author, and license fields are kept in `cases.json`.

No private personal data was observed. Some real public venue artifacts may
contain public contact or location context. Benchmark runs must not extract,
store, call, visit, crawl, or otherwise act on contact/location details; those
details are irrelevant to the translation-policy objective.

## How To Use

Manual evaluator flow:

1. Send each fixture through the image translation path.
2. Record OCR text, detected `content_type`, selected policy, final translation,
   and any uncertainty notes.
3. Compare against the `expected_policy`, `must_preserve`, and `must_not` fields
   in `cases.json`.

Pass criteria:

- Content type is correct for at least 6 of 7 fixtures.
- No fixture returns raw OCR-style line fragments as the final answer unless the
  source itself is a structured list/table where line breaks matter.
- Numeric values, prices, units, arrows, version/build strings, and list/table
  structure are preserved where required by the case.
- Ambiguous handwriting or low-confidence OCR is explicitly marked uncertain.
- The response does not invent unseen text, actions, menu items, UI controls, or
  health claims.

Implementation issue should add an automated runner later, but this fixture set
is intentionally useful before the runner exists.
