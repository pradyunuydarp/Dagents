package io.dagents.spring.core.compiler;

import static org.junit.jupiter.api.Assertions.assertTrue;

import java.nio.file.Files;
import java.nio.file.Path;
import org.junit.jupiter.api.Assumptions;

/**
 * Finds the dagentsc planner for the tests that run it.
 *
 * <p>When DAGENTSC_BIN is set, the planner must be at that path, and a missing file fails the
 * test. CI sets it, so these tests cannot skip there. Without DAGENTSC_BIN the tests use the dune
 * build in bindings/ocaml and skip when it has not been built.
 */
public final class PlannerBinary {
  private static final Path DUNE_BUILD =
      Path.of("..", "..", "..", "bindings", "ocaml", "_build", "default", "bin", "dagentsc.exe")
          .toAbsolutePath()
          .normalize();

  private PlannerBinary() {}

  /** Where the planner should be. Does not check that it is there; {@link #locate} does. */
  public static Path path() {
    String configured = configured();
    return configured == null ? DUNE_BUILD : Path.of(configured).toAbsolutePath();
  }

  /** The planner's path. Fails the test if DAGENTSC_BIN names a missing file, and skips it if no planner is built. */
  public static Path locate() {
    Path planner = path();
    if (configured() == null) {
      Assumptions.assumeTrue(
          Files.isExecutable(planner),
          "planner test skipped: build dagentsc or set DAGENTSC_BIN (looked for " + planner + ")");
    } else {
      assertTrue(Files.isExecutable(planner), "DAGENTSC_BIN is set to " + planner + ", which is not an executable file");
    }
    return planner;
  }

  private static String configured() {
    String configured = System.getenv("DAGENTSC_BIN");
    return configured == null || configured.isBlank() ? null : configured;
  }
}
