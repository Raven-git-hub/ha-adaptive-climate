class AdaptiveClimateCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._nudgeCounts = {};
    this._nudgeTimers = {};
  }

  setConfig(config) {
    if (!config.rooms || !Array.isArray(config.rooms)) {
      throw new Error("Please define a list of rooms in the card config.");
    }
    this.config = config;
  }

  set hass(hass) {
    const dominated = this.config.rooms.flatMap(roomId => [
      `sensor.adaptive_climate_control_${roomId}_acc_${roomId}_predicted_temperature`,
      `sensor.adaptive_climate_control_${roomId}_acc_${roomId}_corrective_state`,
      `sensor.adaptive_climate_control_${roomId}_acc_${roomId}_activity_state`,
      `sensor.adaptive_climate_control_${roomId}_acc_${roomId}_current_period`,
    ]);

    const changed = !this._hass || dominated.some(e => {
      return this._hass?.states[e]?.state !== hass.states[e]?.state;
    });

    this._hass = hass;
    if (changed) this.render();
  }

  getState(entityId) {
    return this._hass?.states[entityId]?.state;
  }

  getAttr(entityId, attr) {
    return this._hass?.states[entityId]?.attributes?.[attr];
  }

  sensorEntityId(roomId, suffix) {
    return `sensor.adaptive_climate_control_${roomId}_acc_${roomId}_${suffix}`;
  }

  getWarnings(roomId) {
    const warnings = [];
    const sensors = this.config.sensor_entities?.[roomId] || [];

    sensors.forEach(sensorId => {
      // Battery check
      const batteryEntity = sensorId.replace("sensor.", "sensor.") + "_battery";
      const battery = parseFloat(this.getState(batteryEntity));
      if (!isNaN(battery) && battery < 20) {
        const name = this.getAttr(sensorId, "friendly_name") || sensorId;
        warnings.push(`Low battery: ${name} (${battery}%)`);
      }

      // Connectivity check — last_updated older than 30 minutes
      const state = this._hass?.states[sensorId];
      if (state) {
        const lastUpdated = new Date(state.last_updated);
        const ageMinutes = (Date.now() - lastUpdated.getTime()) / 60000;
        if (ageMinutes > 30) {
          const name = this.getAttr(sensorId, "friendly_name") || sensorId;
          warnings.push(`No reading: ${name} (${Math.round(ageMinutes)}m ago)`);
        }
      }
    });

    return warnings;
  }

  nudge(roomId, direction) {
    const key = roomId + direction;
    if (!this._nudgeCounts[key]) this._nudgeCounts[key] = 0;
    if (this._nudgeCounts[key] >= 2) return;

    this._nudgeCounts[key]++;

    // Update button pulse class
    const upBtn = this.shadowRoot.getElementById(`up_${roomId}`);
    const dnBtn = this.shadowRoot.getElementById(`dn_${roomId}`);
    if (direction === "up" && upBtn) upBtn.classList.add("pulse-warm");
    if (direction === "dn" && dnBtn) dnBtn.classList.add("pulse-cool");

    // Call HA service
    this._hass.callService("adaptive_climate_control", "nudge_temperature", {
      room_id: roomId,
      direction: direction === "up" ? "up" : "down",
    });

    clearTimeout(this._nudgeTimers[key]);
    this._nudgeTimers[key] = setTimeout(() => {
      this._nudgeCounts[key] = 0;
      if (upBtn) upBtn.classList.remove("pulse-warm");
      if (dnBtn) dnBtn.classList.remove("pulse-cool");
    }, 4000);
  }

  fanSVG() {
    return `<svg width="22" height="22" viewBox="0 0 24 24" fill="none">
      <path d="M12 12 C12 8 15 4 12 2 C9 4 9 8 12 12Z" fill="currentColor" opacity="0.85"/>
      <path d="M12 12 C16 12 20 9 22 12 C20 15 16 15 12 12Z" fill="currentColor" opacity="0.85"/>
      <path d="M12 12 C12 16 9 20 12 22 C15 20 15 16 12 12Z" fill="currentColor" opacity="0.85"/>
      <path d="M12 12 C8 12 4 15 2 12 C4 9 8 9 12 12Z" fill="currentColor" opacity="0.85"/>
      <circle cx="12" cy="12" r="2.2" fill="currentColor"/>
    </svg>`;
  }

  warningSVG() {
    return `<svg viewBox="0 0 24 24" fill="currentColor" width="18" height="18">
      <path d="M1 21L12 2l11 19H1zm11-3h2v-2h-2v2zm0-4h2v-4h-2v4z"/>
    </svg>`;
  }

  roomRowHTML(roomId) {
    const label = (this.config.room_labels?.[roomId] || roomId).replace(/_/g, " ").toUpperCase();
    const tempEntity   = this.sensorEntityId(roomId, "predicted_temperature");
    const stateEntity  = this.sensorEntityId(roomId, "corrective_state");

    const temp         = parseFloat(this.getState(tempEntity)) || "--";
    const corrective   = this.getState(stateEntity) || "idle";
    const acOn         = corrective !== "idle";
    const warnings     = this.getWarnings(roomId);
    const hasWarning   = warnings.length > 0;

    const warningRows  = warnings.map((w, i) => `
      <div class="warning-overlay ${i === 0 && hasWarning ? "" : ""}" id="ov_${roomId}_${i}">
        <span class="warning-overlay-icon">${this.warningSVG()}</span>
        <div class="warning-overlay-text">
          <strong>Warning ${warnings.length > 1 ? `(${i+1} of ${warnings.length})` : ""}</strong>
          ${w}
        </div>
        <button class="close-btn" data-action="${i < warnings.length - 1 ? "next" : "close"}"
          data-room="${roomId}" data-index="${i}">
          ${i < warnings.length - 1 ? "›" : "✕"}
        </button>
      </div>`).join("");

    return `
      <div class="room-row" id="row_${roomId}">
        <span class="room-name">${label}</span>

        <span class="temp-badge ${acOn ? "active" : "idle"}">${temp}°</span>

        <div class="target-controls">
          <span class="target-label">Target</span>
          <button class="nudge-btn" id="up_${roomId}" data-room="${roomId}" data-dir="up">▲</button>
          <button class="nudge-btn" id="dn_${roomId}" data-room="${roomId}" data-dir="dn">▼</button>
        </div>

        <div class="row-icons">
          <div class="fan-icon ${acOn ? "on" : "off"}">${this.fanSVG()}</div>
          ${hasWarning ? `
            <div class="warning-icon" data-room="${roomId}">
              ${this.warningSVG()}
              ${warnings.length > 1 ? `<span class="warning-count">${warnings.length}</span>` : ""}
            </div>` : `<div class="warning-spacer"></div>`}
        </div>

        ${warningRows}
      </div>`;
  }

  render() {
    if (!this._hass) return;

    const rows = this.config.rooms.map(r => this.roomRowHTML(r)).join("");

    this.shadowRoot.innerHTML = `
      <style>
        :host {
          display: block;
          font-family: var(--primary-font-family, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif);
        }

        ha-card {
          padding: 16px;
          box-sizing: border-box;
        }

        .card-header {
          font-size: 13px;
          font-weight: 600;
          letter-spacing: 0.08em;
          color: var(--secondary-text-color);
          text-transform: uppercase;
          margin-bottom: 12px;
        }

        .room-row {
          display: flex;
          align-items: center;
          gap: 10px;
          padding: 10px 0;
          border-bottom: 1px solid var(--divider-color);
          position: relative;
          min-height: 48px;
          overflow: hidden;
          box-sizing: border-box;
        }

        .room-row:last-child { border-bottom: none; }

        .room-name {
          font-size: 12px;
          font-weight: 600;
          letter-spacing: 0.06em;
          color: var(--secondary-text-color);
          text-transform: uppercase;
          width: 88px;
          flex-shrink: 0;
          white-space: nowrap;
          overflow: hidden;
          text-overflow: ellipsis;
        }

        .temp-badge {
          font-size: 15px;
          font-weight: 700;
          padding: 4px 10px;
          border-radius: 6px;
          min-width: 58px;
          text-align: center;
          flex-shrink: 0;
          letter-spacing: 0.02em;
          box-sizing: border-box;
        }

        .temp-badge.idle {
          background: rgba(16, 185, 129, 0.15);
          color: #10b981;
          border: 1px solid rgba(16, 185, 129, 0.3);
        }

        .temp-badge.active {
          background: rgba(245, 158, 11, 0.15);
          color: #f59e0b;
          border: 1px solid rgba(245, 158, 11, 0.3);
        }

        .target-controls {
          display: flex;
          align-items: center;
          gap: 5px;
          flex: 1;
        }

        .target-label {
          font-size: 11px;
          color: var(--secondary-text-color);
          letter-spacing: 0.05em;
          text-transform: uppercase;
          flex-shrink: 0;
        }

        .nudge-btn {
          background: var(--secondary-background-color, #374151);
          border: none;
          border-radius: 6px;
          width: 28px;
          height: 28px;
          display: flex;
          align-items: center;
          justify-content: center;
          cursor: pointer;
          color: var(--primary-text-color);
          font-size: 14px;
          line-height: 1;
          padding: 0;
          transition: background 0.15s, transform 0.1s;
          flex-shrink: 0;
        }

        .nudge-btn:active { transform: scale(0.92); }

        .nudge-btn.pulse-warm {
          animation: pulseWarm 1.2s ease-in-out infinite;
          color: #f59e0b;
        }

        .nudge-btn.pulse-cool {
          animation: pulseCool 1.2s ease-in-out infinite;
          color: #3b82f6;
        }

        @keyframes pulseWarm {
          0%, 100% { background: var(--secondary-background-color, #374151); box-shadow: none; }
          50% { background: rgba(245, 158, 11, 0.25); box-shadow: 0 0 8px rgba(245, 158, 11, 0.4); }
        }

        @keyframes pulseCool {
          0%, 100% { background: var(--secondary-background-color, #374151); box-shadow: none; }
          50% { background: rgba(59, 130, 246, 0.25); box-shadow: 0 0 8px rgba(59, 130, 246, 0.4); }
        }

        .row-icons {
          display: flex;
          align-items: center;
          gap: 6px;
          flex-shrink: 0;
          margin-left: auto;
        }

        .fan-icon {
          width: 24px;
          height: 24px;
          display: flex;
          align-items: center;
          justify-content: center;
          flex-shrink: 0;
        }

        .fan-icon.on { color: #4ade80; animation: spin 1.4s linear infinite; }
        .fan-icon.off { color: #4b5563; }

        @keyframes spin {
          from { transform: rotate(0deg); }
          to   { transform: rotate(360deg); }
        }

        .warning-icon {
          width: 24px;
          height: 24px;
          display: flex;
          align-items: center;
          justify-content: center;
          cursor: pointer;
          border-radius: 6px;
          position: relative;
          flex-shrink: 0;
          color: #f59e0b;
          transition: background 0.15s;
        }

        .warning-icon:hover { background: rgba(245, 158, 11, 0.15); }

        .warning-count {
          position: absolute;
          top: -2px;
          right: -2px;
          background: #f59e0b;
          color: #000;
          font-size: 8px;
          font-weight: 700;
          width: 13px;
          height: 13px;
          border-radius: 50%;
          display: flex;
          align-items: center;
          justify-content: center;
        }

        .warning-spacer { width: 24px; flex-shrink: 0; }

        .warning-overlay {
          position: absolute;
          inset: 0;
          background: var(--card-background-color);
          display: flex;
          align-items: center;
          gap: 10px;
          padding: 0 6px;
          transform: translateX(102%);
          transition: transform 0.22s cubic-bezier(0.4, 0, 0.2, 1);
          z-index: 2;
          box-sizing: border-box;
        }

        .warning-overlay.visible { transform: translateX(0); }

        .warning-overlay-icon { color: #f59e0b; flex-shrink: 0; }

        .warning-overlay-text {
          flex: 1;
          font-size: 12px;
          color: var(--primary-text-color);
          line-height: 1.4;
          min-width: 0;
        }

        .warning-overlay-text strong {
          display: block;
          font-size: 10px;
          color: #f59e0b;
          text-transform: uppercase;
          letter-spacing: 0.06em;
          margin-bottom: 1px;
        }

        .close-btn {
          background: none;
          border: none;
          color: var(--secondary-text-color);
          cursor: pointer;
          padding: 4px 6px;
          border-radius: 4px;
          font-size: 15px;
          line-height: 1;
          flex-shrink: 0;
          transition: color 0.15s;
        }

        .close-btn:hover { color: var(--primary-text-color); }
      </style>

      <ha-card>
        <div class="card-header">${this.config.title || "Climate Control"}</div>
        ${rows}
      </ha-card>`;

    // Nudge buttons
    this.shadowRoot.querySelectorAll(".nudge-btn").forEach(btn => {
      btn.addEventListener("click", () => {
        this.nudge(btn.dataset.room, btn.dataset.dir);
      });
    });

    // Warning icons — show first overlay
    this.shadowRoot.querySelectorAll(".warning-icon").forEach(icon => {
      icon.addEventListener("click", () => {
        const roomId = icon.dataset.room;
        const first = this.shadowRoot.getElementById(`ov_${roomId}_0`);
        if (first) first.classList.add("visible");
      });
    });

    // Close / next buttons on overlays
    this.shadowRoot.querySelectorAll(".close-btn").forEach(btn => {
      btn.addEventListener("click", () => {
        const roomId = btn.dataset.room;
        const index  = parseInt(btn.dataset.index);
        const action = btn.dataset.action;
        const current = this.shadowRoot.getElementById(`ov_${roomId}_${index}`);
        if (current) current.classList.remove("visible");
        if (action === "next") {
          const next = this.shadowRoot.getElementById(`ov_${roomId}_${index + 1}`);
          if (next) next.classList.add("visible");
        }
      });
    });
  }

  getCardSize() { return this.config.rooms?.length || 1; }

  static getConfigElement() {
    return document.createElement("adaptive-climate-card-editor");
  }

  static getStubConfig() {
    return { title: "Climate Control", rooms: [], sensor_entities: {}, room_labels: {} };
  }
}

customElements.define("adaptive-climate-card", AdaptiveClimateCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "adaptive-climate-card",
  name: "Adaptive Climate Control",
  description: "Displays room temperatures and allows comfort nudging for the Adaptive Climate Control integration.",
});
