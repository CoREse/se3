<!-- spec-format: v1 -->
<!-- domain: server/frontend -->

# web-console-modals Specification

## Purpose

Define the dismissal contract shared by the web console's modal dialogs — the
center-screen popups (`#issue-modal`, `#issue-action-modal`, `#end-session-modal`,
`#issue-launch-modal`, `#keys-modal`, `#users-modal`, `#new-task-modal`) rendered
by the bundled static frontend (`src/se3/server/static/app.js`). This is a
cross-cutting interaction convention referenced by every console surface that
opens a modal, so it lives in one place rather than being restated per surface.

## Requirements

### Requirement: Explicit-Control-Only Modal Dismissal

A web-console modal dialog MUST NOT be dismissed by a click on its backdrop (the
dimmed area outside the modal panel). A modal MUST be closeable ONLY through an
explicit control it owns — its × (close) button and/or its Cancel button (and,
where applicable, completing the modal's confirm action). This prevents the
common data-loss accident where a user, partway through filling in a modal form
(most notably the new-issue / new-task form), clicks just outside the panel and
silently loses everything they had typed.

Concretely, none of the seven modal containers — `issue-modal`,
`issue-action-modal`, `end-session-modal`, `issue-launch-modal`, `keys-modal`,
`users-modal`, `new-task-modal` — registers a backdrop click-to-close listener
(the previously-used pattern was
`<modal>.addEventListener("click", e => { if (e.target.id === "<modal>") close…() })`).
Each modal nonetheless retains its explicit close-button bindings: the ×
control and the Cancel control each still invoke that modal's close handler.

This contract governs only the center-screen modal dialogs. It does NOT change
two intentionally-different overlay dismissals that remain backdrop/outside-click
dismissible by design: the phone-portrait off-canvas `#flow-view` sidebar drawer
(`flow-sidebar-backdrop`, governed by the *Mobile Portrait Responsive Layout*
requirement in the `running-flow-console` spec) and the top-bar overflow
nav-menu's outside-click dismissal. The Escape-key / history-back close paths
for views that define them are likewise unaffected.

#### Scenario: Clicking the backdrop does not close a modal

- **GIVEN** any of the seven console modals is open with user-entered content in
  its form (e.g. the new-issue `issue-modal` with a typed description)
- **WHEN** the user clicks the dimmed backdrop area outside the modal panel
- **THEN** the modal stays open and its entered content is preserved
- **AND** no close handler is invoked by that backdrop click

#### Scenario: Explicit close controls still dismiss the modal

- **GIVEN** any of the seven console modals is open
- **WHEN** the user clicks the modal's × button or its Cancel button
- **THEN** the modal is dismissed via its own close handler

#### Scenario: Off-canvas drawer and nav-menu dismissal are unchanged

- **GIVEN** the phone-portrait `#flow-view` sidebar drawer or the top-bar
  overflow nav-menu is open
- **WHEN** the user taps the drawer backdrop or clicks outside the nav-menu
- **THEN** that overlay still dismisses, because backdrop / outside-click
  dismissal for these is a deliberate, separate behavior from the modal-dialog
  contract above
