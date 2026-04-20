package io.dagents.spring.control.api;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.put;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import io.dagents.spring.control.application.ControlPlaneService;
import io.dagents.spring.control.infrastructure.InMemoryAgentRegistryRepository;
import io.dagents.spring.control.infrastructure.InMemoryDeploymentRepository;
import io.dagents.spring.control.infrastructure.InMemoryTelemetryRepository;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

class AgentControlControllerTest {
  private MockMvc mockMvc;

  @BeforeEach
  void setUp() {
    ControlPlaneService controlPlaneService = new ControlPlaneService(
        new InMemoryAgentRegistryRepository(),
        new InMemoryDeploymentRepository(),
        new InMemoryTelemetryRepository()
    );
    mockMvc = MockMvcBuilders.standaloneSetup(new AgentControlController(controlPlaneService))
        .setControllerAdvice(new ApiExceptionHandler())
        .build();
  }

  @Test
  void rejectsPathAndBodyAgentMismatch() throws Exception {
    mockMvc.perform(put("/api/v1/agents/lma-1/registration")
            .contentType(MediaType.APPLICATION_JSON)
            .content("""
                {
                  "agent": {
                    "agentId": "lma-2",
                    "workspaceId": "alpha",
                    "name": "local-agent",
                    "agentType": "LMA"
                  },
                  "scope": {"tenant": "alpha"},
                  "version": "0.1.0",
                  "capabilities": ["monitoring"]
                }
                """))
        .andExpect(status().isUnprocessableEntity())
        .andExpect(jsonPath("$.code").value("validation_error"));
  }

  @Test
  void registersAgentWhenPayloadIsValid() throws Exception {
    mockMvc.perform(put("/api/v1/agents/lma-1/registration")
            .contentType(MediaType.APPLICATION_JSON)
            .content("""
                {
                  "agent": {
                    "agentId": "lma-1",
                    "workspaceId": "alpha",
                    "name": "local-agent",
                    "agentType": "LMA"
                  },
                  "scope": {"tenant": "alpha"},
                  "version": "0.1.0",
                  "capabilities": ["monitoring"]
                }
                """))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$.accepted").value(true))
        .andExpect(jsonPath("$.capabilities[0]").value("monitoring"));
  }
}
