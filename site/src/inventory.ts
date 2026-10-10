/**
 * The API reference data.
 *
 * `docs/reference/service-inventory.json` is generated from the services' own
 * routing tables by `scripts/service_inventory.py`, and a test fails if it falls
 * out of date. The site reads it directly, so the reference always matches the
 * code.
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
      "The LMA runs at each data source and the GMA coordinates them. Both offer short paths such as /health and versioned paths such as /api/v1/health; each pair runs the same handler."
  },
  {
    role: "framework-service",
    label: "Framework services",
    blurb:
      "The services an application calls for profiling, pipelines, model routing and deployment manifests. All paths are versioned. The two Spring Boot services offer the same APIs to JVM applications."
  },
  {
    role: "demo-app",
    label: "Demo apps",
    blurb: "Example applications built on the framework. They are not part of it."
  }
];

/** Services carrying a given role, in inventory order. */
export function servicesWithRole(role: ServiceEntry["role"]): ServiceEntry[] {
  return inventory.services.filter((service) => service.role === role);
}
