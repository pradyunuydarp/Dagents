package io.dagents.spring.core.compiler;

import io.dagents.spring.common.contracts.WorkloadCompileRequest;
import io.dagents.spring.common.contracts.WorkloadPlanResponse;

public interface ManifestCompilerGateway {
  WorkloadPlanResponse compile(WorkloadCompileRequest request);
}
