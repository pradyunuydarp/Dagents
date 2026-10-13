import { useCallback, useEffect, useState } from "react";

import { getJson } from "./api";

export interface Resource<T> {
  data: T | null;
  error: unknown;
  loading: boolean;
  /** Fetch again, keeping the current data on screen until the answer arrives. */
  reload: () => void;
}

interface Loaded<T> {
  path: string | null;
  value: T | null;
  error: unknown;
}

/**
 * GET one JSON document and keep it in state.
 *
 * `path` null means "not yet": nothing is fetched. A newer request always wins,
 * so a slow answer for an old path cannot overwrite a newer one, and data is
 * only returned for the path it was fetched for, so one hospital's numbers
 * never show under another hospital's name.
 */
export function useResource<T>(path: string | null): Resource<T> {
  const [loaded, setLoaded] = useState<Loaded<T>>({ path: null, value: null, error: null });
  const [loading, setLoading] = useState(path !== null);
  const [generation, setGeneration] = useState(0);

  useEffect(() => {
    if (path === null) return;
    let current = true;
    setLoading(true);
    getJson<T>(path)
      .then((value) => {
        if (current) setLoaded({ path, value, error: null });
      })
      .catch((exc: unknown) => {
        if (current) setLoaded((before) => ({ path, value: before.path === path ? before.value : null, error: exc }));
      })
      .finally(() => {
        if (current) setLoading(false);
      });
    return () => {
      current = false;
    };
  }, [path, generation]);

  const reload = useCallback(() => setGeneration((value) => value + 1), []);
  const mine = loaded.path === path;
  return { data: mine ? loaded.value : null, error: mine ? loaded.error : null, loading, reload };
}
