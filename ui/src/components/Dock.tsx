/**
 * Bottom dock. Phase B mounts the timeline here (`#timeline-dock-body`); for now it is an
 * intentionally empty placeholder so the grid area and height are already in place.
 */
export function Dock() {
  return (
    <section className="dock" id="timeline-dock" aria-label="Timeline">
      <header className="panel-head">
        <h2 className="hud-title">Timeline</h2>
      </header>
      <div className="dock-body" id="timeline-dock-body" />
    </section>
  );
}
