# Option Discovery Skill

Purpose: read dropdown, radio, checkbox, and custom combobox choices before
selecting an answer.

Inputs:
- Field metadata and DOM locator.

Outputs:
- Normalized option labels and values.

Rules:
- For optioned fields, choose an actual option label.
- If options are hidden, click/open the control and wait for visible options.
- For ATSes that require actual option clicks, do not rely on setting text only.

Code:
- Chrome extension: `discoverDynamicDropdownOptions`, `fillCombobox`.
- Playwright agent: `dynamic_options_for`, `select_dynamic_option`.
