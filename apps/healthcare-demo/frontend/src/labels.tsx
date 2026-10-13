import { createContext, useContext } from "react";

import { fieldLabel } from "./format";

/**
 * The field labels from `/api/v1/overview`, for every page.
 *
 * The backend reads them from the feature contract, the same source as the
 * entry form, so a field has one name everywhere. Until the overview answers,
 * `fieldLabel` falls back to its own list.
 */
const FieldLabels = createContext<Record<string, string> | null>(null);

export const FieldLabelsProvider = FieldLabels.Provider;

/** A function that names a field the way the backend does. */
export function useFieldLabel(): (name: string) => string {
  const labels = useContext(FieldLabels);
  return (name: string) => fieldLabel(name, labels);
}
