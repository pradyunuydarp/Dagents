package io.dagents.spring.control.infrastructure;

import io.dagents.spring.common.contracts.TelemetryEnvelope;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import org.springframework.stereotype.Repository;

@Repository
public class InMemoryTelemetryRepository {
  private final CopyOnWriteArrayList<TelemetryEnvelope> events = new CopyOnWriteArrayList<>();

  public String append(TelemetryEnvelope envelope) {
    events.add(envelope);
    return "telemetry-" + events.size();
  }

  public List<TelemetryEnvelope> recent(int limit) {
    List<TelemetryEnvelope> copy = new ArrayList<>(events);
    int start = Math.max(copy.size() - limit, 0);
    return copy.subList(start, copy.size());
  }
}
