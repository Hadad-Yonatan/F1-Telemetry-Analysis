"use strict";

(() => {
  const payload = JSON.parse(document.getElementById("report-data").textContent);
  const analysis = payload.analysis;
  const distances = payload.traces.distance_m;
  const reference = payload.traces.reference;
  const comparisonDocuments = new Map(
    analysis.comparisons.map((value) => [value.comparison_lap, value])
  );
  const comparisonTraces = new Map(
    payload.traces.comparisons.map((value) => [value.lap_number, value])
  );
  const presentations = new Map(
    payload.presentation.comparisons.map((value) => [value.comparison_lap, value])
  );
  const labels = {
    speed_kph: ["Speed", "km/h"],
    delta_s: ["Delta", "s"],
    throttle: ["Throttle", ""],
    brake: ["Brake", ""],
    steering: ["Steering", ""],
    gear: ["Gear", ""],
  };
  const state = {
    comparisonLap: payload.presentation.initial_comparison_lap,
    cursorIndex: 0,
    rangeStart: distances[0],
    rangeEnd: distances[distances.length - 1],
    selectedRegion: null,
    drag: null,
  };

  const select = document.getElementById("comparison-select");
  const chartStack = document.getElementById("chart-stack");
  const regionList = document.getElementById("regions");
  const resetButton = document.getElementById("reset-zoom");

  function appendDefinition(list, term, value) {
    const container = document.createElement("div");
    const dt = document.createElement("dt");
    const dd = document.createElement("dd");
    dt.textContent = term;
    dd.textContent = String(value);
    container.append(dt, dd);
    list.append(container);
  }

  function formatLapTime(milliseconds) {
    if (!milliseconds) return "—";
    const minutes = Math.floor(milliseconds / 60000);
    const seconds = (milliseconds % 60000) / 1000;
    return `${minutes}:${seconds.toFixed(3).padStart(6, "0")}`;
  }

  function signed(value, digits, suffix = "") {
    const sign = value >= 0 ? "+" : "";
    return `${sign}${Number(value).toFixed(digits)}${suffix}`;
  }

  function initializeIdentity() {
    document.getElementById("capture-name").textContent = analysis.capture.basename;
    const selectedTrack = analysis.corner_catalog.selected_track;
    const summary = document.getElementById("session-summary");
    appendDefinition(summary, "Track", selectedTrack ? selectedTrack.display_name : `Track ${analysis.session.track_id}`);
    appendDefinition(summary, "Length", `${analysis.session.track_length_m} m`);
    appendDefinition(summary, "Session UID", payload.presentation.session_uid_label);
    appendDefinition(summary, "Reference", `Lap ${analysis.selected_reference_lap}`);
    appendDefinition(summary, "Algorithm", analysis.algorithm.version);
    appendDefinition(summary, "Catalog", analysis.corner_catalog.catalog_id || analysis.corner_catalog.status);
    appendDefinition(summary, "Joined samples", analysis.session.normalization.samples_joined);
    appendDefinition(summary, "Decode errors", analysis.session.normalization.decode_errors);
    const context = analysis.session_context;
    if (context) {
      const settings = context.settings;
      appendDefinition(summary, "Session type", settings ? settings.session_type : "Unavailable");
      appendDefinition(summary, "Weather code", settings ? settings.weather : "Unavailable");
      appendDefinition(summary, "Track temperature", settings ? `${settings.track_temperature_c} °C` : "Unavailable");
      appendDefinition(summary, "Air temperature", settings ? `${settings.air_temperature_c} °C` : "Unavailable");
    }
    appendDefinition(
      summary,
      "Unmatched frames",
      analysis.session.normalization.unmatched_lap_packets + analysis.session.normalization.unmatched_telemetry_packets
    );

    const body = document.getElementById("eligibility-body");
    analysis.lap_eligibility.forEach((lap) => {
      const row = document.createElement("tr");
      const values = [
        lap.lap_number,
        formatLapTime(lap.official_lap_time_ms),
        ...lap.sector_times_ms.map(formatLapTime),
        lap.reason,
      ];
      values.forEach((value, index) => {
        const cell = document.createElement(index === 0 ? "th" : "td");
        if (index === 0) cell.scope = "row";
        cell.textContent = String(value);
        row.append(cell);
      });
      body.append(row);
    });

    analysis.comparisons.forEach((comparison) => {
      const option = document.createElement("option");
      option.value = String(comparison.comparison_lap);
      option.textContent = `Lap ${comparison.comparison_lap} (${signed(comparison.official_delta_s, 3, " s")})`;
      select.append(option);
    });
    select.value = String(state.comparisonLap);
  }

  function currentDocument() {
    return comparisonDocuments.get(state.comparisonLap);
  }

  function currentTrace() {
    return comparisonTraces.get(state.comparisonLap);
  }

  function renderComparisonSummary() {
    const comparison = currentDocument();
    document.body.dataset.comparisonLap = String(state.comparisonLap);
    document.getElementById("comparison-heading").textContent =
      `Lap ${comparison.comparison_lap} versus lap ${comparison.reference_lap}`;
    const summary = document.getElementById("delta-summary");
    summary.replaceChildren();
    appendDefinition(summary, "Official", signed(comparison.official_delta_s, 3, " s"));
    comparison.sector_deltas_s.forEach((value, index) => {
      appendDefinition(summary, `Sector ${index + 1}`, signed(value, 3, " s"));
    });
    const presentation = presentations.get(state.comparisonLap);
    document.getElementById("csv-link").href = presentation.artifact_links.csv;
    document.getElementById("svg-link").href = presentation.artifact_links.svg;
  }

  function eventByIndex(events, eventIndex) {
    return events.find((event) => event.event_index === eventIndex);
  }

  function eventSummary(event) {
    const throttle = event.throttle_reapplication_distance_m === null
      ? "Sustained full throttle not detected"
      : `Full throttle ${event.throttle_reapplication_distance_m.toFixed(3)} m`;
    return [
      `Brake ${event.braking_start_distance_m.toFixed(3)}–${event.braking_end_distance_m.toFixed(3)} m`,
      `distance ${event.braking_distance_m.toFixed(3)} m`,
      `peak ${event.peak_brake.toFixed(6)}`,
      `minimum ${event.minimum_speed_kph.toFixed(6)} km/h at ${event.minimum_speed_distance_m.toFixed(3)} m`,
      throttle,
      `coast ${event.coast_distance_m.toFixed(3)} m`,
    ].join("; ");
  }

  function addEventBlock(parent, heading, event, evidence) {
    const block = document.createElement("div");
    block.className = "event-block";
    const title = document.createElement("strong");
    title.textContent = heading;
    const measurements = document.createElement("p");
    measurements.textContent = eventSummary(event);
    block.append(title, measurements);
    if (evidence.length) {
      const evidenceList = document.createElement("ul");
      evidenceList.className = "evidence-list";
      evidence.forEach((label) => {
        const item = document.createElement("li");
        item.textContent = label;
        evidenceList.append(item);
      });
      block.append(evidenceList);
    }
    parent.append(block);
  }

  function renderRegionDetails(parent, comparison, region) {
    const metrics = document.createElement("dl");
    const evidence = region.evidence;
    [
      ["Reference minimum speed", `${evidence.reference_min_speed_kph.toFixed(6)} km/h`],
      ["Comparison minimum speed", `${evidence.comparison_min_speed_kph.toFixed(6)} km/h`],
      ["Minimum-speed difference", signed(evidence.minimum_speed_difference_kph, 6, " km/h")],
      ["Reference mean throttle", evidence.reference_mean_throttle.toFixed(6)],
      ["Comparison mean throttle", evidence.comparison_mean_throttle.toFixed(6)],
      ["Reference mean brake", evidence.reference_mean_brake.toFixed(6)],
      ["Comparison mean brake", evidence.comparison_mean_brake.toFixed(6)],
    ].forEach(([term, value]) => {
      const dt = document.createElement("dt");
      const dd = document.createElement("dd");
      dt.textContent = term;
      dd.textContent = value;
      metrics.append(dt, dd);
    });
    parent.append(metrics);

    const aggregate = document.createElement("ul");
    aggregate.className = "evidence-list";
    evidence.labels.forEach((label) => {
      const item = document.createElement("li");
      item.textContent = label;
      aggregate.append(item);
    });
    parent.append(aggregate);

    const events = comparison.driving_events;
    if (region.matched_event_index !== null) {
      const match = events.matches.find((value) => value.match_index === region.matched_event_index);
      const referenceEvent = eventByIndex(events.reference, match.reference_event_index);
      const comparedEvent = eventByIndex(events.comparison, match.comparison_event_index);
      addEventBlock(parent, "Reference event", referenceEvent, []);
      addEventBlock(parent, "Comparison event", comparedEvent, match.evidence_labels);
      return;
    }

    let associated = false;
    region.unmatched_reference_event_indices.forEach((eventIndex) => {
      associated = true;
      addEventBlock(parent, "Reference-only event", eventByIndex(events.reference, eventIndex), ["No comparison event matched within the configured distance."]);
    });
    region.unmatched_comparison_event_indices.forEach((eventIndex) => {
      associated = true;
      addEventBlock(parent, "Comparison-only event", eventByIndex(events.comparison, eventIndex), ["No reference event matched within the configured distance."]);
    });
    if (!associated) {
      const empty = document.createElement("p");
      empty.textContent = "No qualifying braking event overlaps this region.";
      parent.append(empty);
    }
  }

  function renderRegions() {
    const comparison = currentDocument();
    const presentation = presentations.get(state.comparisonLap);
    regionList.replaceChildren();
    if (!comparison.loss_regions.length) {
      const empty = document.createElement("p");
      empty.textContent = "No loss regions met the configured thresholds.";
      regionList.append(empty);
      return;
    }
    comparison.loss_regions.forEach((region, index) => {
      const item = document.createElement("article");
      item.className = "region-item";
      item.dataset.regionIndex = String(index);
      if (state.selectedRegion === index) item.classList.add("selected");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "region-button";
      button.dataset.regionIndex = String(index);
      button.setAttribute("aria-label", `Zoom to loss region ${index + 1}: ${presentation.region_labels[index]}`);
      const title = document.createElement("span");
      title.className = "region-title";
      title.textContent = `${index + 1}. ${presentation.region_labels[index]}`;
      const meta = document.createElement("span");
      meta.className = "region-meta";
      meta.textContent = `Sector ${region.sector} · ${region.start_distance_m.toFixed(3)}–${region.end_distance_m.toFixed(3)} m · ${signed(region.time_loss_s, 3, " s")}`;
      button.append(title, meta);
      const details = document.createElement("div");
      details.className = "region-details";
      renderRegionDetails(details, comparison, region);
      item.append(button, details);
      regionList.append(item);
    });
  }

  function lowerBound(value) {
    let low = 0;
    let high = distances.length;
    while (low < high) {
      const middle = (low + high) >> 1;
      if (distances[middle] < value) low = middle + 1;
      else high = middle;
    }
    return Math.min(low, distances.length - 1);
  }

  function nearestIndex(value) {
    const upper = lowerBound(value);
    if (upper === 0) return 0;
    return value - distances[upper - 1] <= distances[upper] - value ? upper - 1 : upper;
  }

  function svgNode(svg, name, attributes, text) {
    const node = document.createElementNS(svg.namespaceURI, name);
    Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, String(value)));
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function channelBounds(channel, startIndex, endIndex, trace) {
    if (channel === "throttle" || channel === "brake") return [0, 1];
    if (channel === "steering") return [-1, 1];
    if (channel === "gear") return [-1, 8];
    const arrays = channel === "delta_s" ? [trace.delta_s] : [reference[channel], trace[channel]];
    let minimum = channel === "delta_s" ? 0 : Infinity;
    let maximum = channel === "delta_s" ? 0 : -Infinity;
    arrays.forEach((array) => {
      for (let index = startIndex; index <= endIndex; index += 1) {
        minimum = Math.min(minimum, array[index]);
        maximum = Math.max(maximum, array[index]);
      }
    });
    if (minimum === maximum) return [minimum - 1, maximum + 1];
    const padding = (maximum - minimum) * 0.08;
    return [minimum - padding, maximum + padding];
  }

  function pathFor(array, startIndex, endIndex, xScale, yScale) {
    const stride = Math.max(1, Math.ceil((endIndex - startIndex + 1) / 1000));
    const points = [];
    for (let index = startIndex; index <= endIndex; index += stride) {
      points.push(`${points.length ? "L" : "M"}${xScale(distances[index]).toFixed(2)},${yScale(array[index]).toFixed(2)}`);
    }
    if ((endIndex - startIndex) % stride !== 0) {
      points.push(`L${xScale(distances[endIndex]).toFixed(2)},${yScale(array[endIndex]).toFixed(2)}`);
    }
    return points.join(" ");
  }

  function renderChart(svg) {
    const channel = svg.dataset.channel;
    const trace = currentTrace();
    const startIndex = lowerBound(state.rangeStart);
    const endIndex = Math.max(startIndex, lowerBound(state.rangeEnd));
    const [minimum, maximum] = channelBounds(channel, startIndex, endIndex, trace);
    const left = 54;
    const right = 884;
    const top = 12;
    const bottom = 122;
    const xScale = (value) => left + (value - state.rangeStart) / (state.rangeEnd - state.rangeStart) * (right - left);
    const yScale = (value) => bottom - (value - minimum) / (maximum - minimum) * (bottom - top);
    svg.replaceChildren();
    svg.append(
      svgNode(svg, "line", {x1: left, y1: top, x2: left, y2: bottom, class: "chart-grid"}),
      svgNode(svg, "line", {x1: left, y1: bottom, x2: right, y2: bottom, class: "chart-grid"}),
      svgNode(svg, "text", {x: 2, y: top + 5, class: "chart-label"}, maximum.toFixed(channel === "gear" ? 0 : 2)),
      svgNode(svg, "text", {x: 2, y: bottom, class: "chart-label"}, minimum.toFixed(channel === "gear" ? 0 : 2)),
      svgNode(svg, "text", {x: left, y: 144, class: "chart-label"}, `${state.rangeStart.toFixed(0)} m`),
      svgNode(svg, "text", {x: right, y: 144, "text-anchor": "end", class: "chart-label"}, `${state.rangeEnd.toFixed(0)} m`)
    );
    if (minimum < 0 && maximum > 0) {
      svg.append(svgNode(svg, "line", {x1: left, y1: yScale(0), x2: right, y2: yScale(0), class: "chart-zero"}));
    }
    if (channel === "delta_s") {
      svg.append(svgNode(svg, "path", {d: pathFor(trace.delta_s, startIndex, endIndex, xScale, yScale), class: "chart-line line-delta"}));
    } else {
      svg.append(
        svgNode(svg, "path", {d: pathFor(reference[channel], startIndex, endIndex, xScale, yScale), class: "chart-line line-reference"}),
        svgNode(svg, "path", {d: pathFor(trace[channel], startIndex, endIndex, xScale, yScale), class: "chart-line line-comparison"})
      );
    }
    const cursorDistance = distances[state.cursorIndex];
    const cursorX = xScale(Math.min(state.rangeEnd, Math.max(state.rangeStart, cursorDistance)));
    svg.append(svgNode(svg, "line", {x1: cursorX, y1: top, x2: cursorX, y2: bottom, class: "chart-cursor"}));
  }

  function renderCharts() {
    chartStack.dataset.rangeStart = state.rangeStart.toFixed(3);
    chartStack.dataset.rangeEnd = state.rangeEnd.toFixed(3);
    chartStack.dataset.cursorDistance = distances[state.cursorIndex].toFixed(3);
    document.querySelectorAll(".telemetry-chart").forEach(renderChart);
    renderCursorValues();
  }

  function displayValue(channel, value) {
    if (channel === "gear") return String(value);
    return Number(value).toFixed(6);
  }

  function renderCursorValues() {
    const trace = currentTrace();
    const index = state.cursorIndex;
    document.getElementById("cursor-distance").textContent = `Cursor: ${distances[index].toFixed(3)} m`;
    const body = document.getElementById("cursor-values");
    body.replaceChildren();
    const rows = [
      ["Elapsed", reference.elapsed_s[index].toFixed(6), trace.elapsed_s[index].toFixed(6), "—"],
      ["Speed", displayValue("speed_kph", reference.speed_kph[index]), displayValue("speed_kph", trace.speed_kph[index]), "—"],
      ["Delta", "0.000000", trace.delta_s[index].toFixed(6), trace.delta_s[index].toFixed(6)],
      ["Throttle", displayValue("throttle", reference.throttle[index]), displayValue("throttle", trace.throttle[index]), "—"],
      ["Brake", displayValue("brake", reference.brake[index]), displayValue("brake", trace.brake[index]), "—"],
      ["Steering", displayValue("steering", reference.steering[index]), displayValue("steering", trace.steering[index]), "—"],
      ["Gear", displayValue("gear", reference.gear[index]), displayValue("gear", trace.gear[index]), "—"],
    ];
    rows.forEach((values) => {
      const row = document.createElement("tr");
      values.forEach((value, column) => {
        const cell = document.createElement(column === 0 ? "th" : "td");
        if (column === 0) cell.scope = "row";
        cell.textContent = value;
        row.append(cell);
      });
      body.append(row);
    });
  }

  function resetView() {
    state.rangeStart = distances[0];
    state.rangeEnd = distances[distances.length - 1];
    state.cursorIndex = 0;
    state.selectedRegion = null;
    renderRegions();
    renderCharts();
  }

  function distanceFromPointer(svg, clientX) {
    const rectangle = svg.getBoundingClientRect();
    const fraction = Math.min(1, Math.max(0, (clientX - rectangle.left) / rectangle.width));
    return state.rangeStart + fraction * (state.rangeEnd - state.rangeStart);
  }

  function setCursor(distance) {
    state.cursorIndex = nearestIndex(distance);
    renderCharts();
  }

  function setDragCursor(distance) {
    state.cursorIndex = nearestIndex(distance);
    chartStack.dataset.cursorDistance = distances[state.cursorIndex].toFixed(3);
    renderCursorValues();
  }

  document.querySelectorAll(".telemetry-chart").forEach((svg) => {
    svg.addEventListener("pointerdown", (event) => {
      svg.setPointerCapture(event.pointerId);
      const distance = distanceFromPointer(svg, event.clientX);
      state.drag = {pointerId: event.pointerId, start: distance, current: distance};
      setDragCursor(distance);
    });
    svg.addEventListener("pointermove", (event) => {
      const distance = distanceFromPointer(svg, event.clientX);
      if (state.drag && state.drag.pointerId === event.pointerId) {
        state.drag.current = distance;
        setDragCursor(distance);
      } else {
        setCursor(distance);
      }
    });
    svg.addEventListener("pointerup", (event) => {
      if (!state.drag || state.drag.pointerId !== event.pointerId) return;
      const start = state.drag.start;
      const end = distanceFromPointer(svg, event.clientX);
      state.drag = null;
      if (Math.abs(end - start) >= 10) {
        state.rangeStart = Math.min(start, end);
        state.rangeEnd = Math.max(start, end);
        state.cursorIndex = nearestIndex(state.rangeStart);
        state.selectedRegion = null;
        renderRegions();
        renderCharts();
      }
    });
    svg.addEventListener("pointercancel", () => { state.drag = null; });
  });

  chartStack.addEventListener("keydown", (event) => {
    let targetDistance = distances[state.cursorIndex];
    if (event.key === "ArrowLeft") targetDistance -= event.shiftKey ? 10 : 1;
    else if (event.key === "ArrowRight") targetDistance += event.shiftKey ? 10 : 1;
    else if (event.key === "Home") targetDistance = distances[0];
    else if (event.key === "End") targetDistance = distances[distances.length - 1];
    else if (event.key === "Escape") {
      event.preventDefault();
      resetView();
      return;
    } else return;
    event.preventDefault();
    setCursor(Math.min(distances[distances.length - 1], Math.max(distances[0], targetDistance)));
  });

  regionList.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-region-index]");
    if (!button) return;
    const index = Number(button.dataset.regionIndex);
    const region = currentDocument().loss_regions[index];
    state.rangeStart = Math.max(distances[0], region.start_distance_m - 100);
    state.rangeEnd = Math.min(distances[distances.length - 1], region.end_distance_m + 100);
    state.cursorIndex = nearestIndex(region.start_distance_m);
    state.selectedRegion = index;
    renderRegions();
    renderCharts();
    chartStack.focus();
  });

  select.addEventListener("change", () => {
    state.comparisonLap = Number(select.value);
    state.rangeStart = distances[0];
    state.rangeEnd = distances[distances.length - 1];
    state.cursorIndex = 0;
    state.selectedRegion = null;
    renderComparisonSummary();
    renderRegions();
    renderCharts();
  });

  document.querySelectorAll(".channel-controls input").forEach((input) => {
    input.addEventListener("change", () => {
      document.querySelector(`.chart-panel[data-channel="${input.value}"]`).hidden = !input.checked;
    });
  });
  resetButton.addEventListener("click", resetView);

  initializeIdentity();
  renderComparisonSummary();
  renderRegions();
  renderCharts();
})();
