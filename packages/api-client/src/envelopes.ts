import type { components } from "./generated/schema";

// FastAPI titles the generic envelope as ApiEnvelope[T], which the exporter
// emits as ApiEnvelope_<T>_ component schema names.
export type ApiErrorBody = components["schemas"]["ApiErrorBody"];
export type LivenessEnvelope = components["schemas"]["ApiEnvelope_LivenessData_"];
export type ReadinessEnvelope = components["schemas"]["ApiEnvelope_ReadinessData_"];
