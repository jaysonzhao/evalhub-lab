import { describe, expect, it } from "vitest";
import { formatMetric, terminalOutcome } from "./logic";

describe("metric labels and terminal outcomes", () => {
  it("keeps zero percent numeric and does not guess ratio scaling", () => {
    expect(formatMetric({ value: 0, unit: "%" })).toBe("0.00 %");
    expect(formatMetric({ value: 0.5, unit: "%" })).toBe("0.50 %");
  });

  it("distinguishes unavailable latency from measured zero", () => {
    expect(formatMetric({ value: 0, unit: "ms", status: "measurement_unknown" })).toBe("Measurement availability unknown");
  });

  it("parses imported string false as false", () => {
    expect(terminalOutcome({ correct: "False", parse_ok: "true", transport_ok: "true" })).toBe("wrong");
    expect(terminalOutcome({ correct: "false", parse_ok: "false", transport_ok: "false" })).toBe("transport_failed");
  });
});
