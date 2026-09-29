/**
 * The framework's published HTTP contract, read from the generated artifact.
 *
 * `docs/reference/service-inventory.json` is produced by
 * `scripts/service_inventory.py` from the live FastAPI routing tables and the
 * Spring controllers, and `tests/test_service_inventory.py` fails when it drifts
 * from the code. Importing it here means this page cannot describe an endpoint
 * the services do not serve — the alternative, a hand-written API table on a
 * docs site, is wrong within a week.
 */

import document from "../../docs/reference/service-inventory.json";

export interface Endpoint {
  method: string;
  path: string;
  handler: string | null;
  status_code: number | null;
  summary: string | null;
  alias_of: { method: string; path: string } | null;
}

export interface ServiceEntry {
  name: string;
  language: "python" | "java";
  framework: string;
  role: "agent" | "framework-service" | "demo-app";
  port: number;
  title: string;
  endpoints: Endpoint[];
}

export interface Inventory {
  framework: string;
  generated_by: string;
  endpoint_count: number;
  services: ServiceEntry[];
}

export const inventory = document as unknown as Inventory;

/** Human label for each role, in the order the site presents them. */
export const ROLE_ORDER: { role: ServiceEntry["role"]; label: string; blurb: string }[] = [
  {
    role: "agent",
    label: "Agents",
    blurb:
      "One LMA per source boundary; one GMA coordinating them. Both expose legacy short paths and versioned equivalents, and the versioned handler calls the legacy one so the two cannot drift."
  },
  {
    role: "framework-service",
    label: "Framework services",
    blurb:
      "The surfaces a consumer backend calls instead of rebuilding profiling, orchestration, routing and manifest generation. Uniformly versioned. The two Spring services mirror the control-plane and core surfaces for consumers integrating through the JVM."
  },
  {
    role: "demo-app",
    label: "Demo apps",
    blurb:
      "Not part of the framework. They are here to prove a real app can consume it — and to mark the boundary from two directions."
  }
];

/** Services carrying a given role, in inventory order. */
export function servicesWithRole(role: ServiceEntry["role"]): ServiceEntry[] {
  return inventory.services.filter((service) => service.role === role);
}
