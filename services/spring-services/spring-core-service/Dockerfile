FROM maven:3.9.11-eclipse-temurin-25 AS spring-builder

WORKDIR /workspace

COPY services/spring-services/pom.xml services/spring-services/pom.xml
COPY services/spring-services/dagents-spring-common/pom.xml services/spring-services/dagents-spring-common/pom.xml
COPY services/spring-services/spring-core-service/pom.xml services/spring-services/spring-core-service/pom.xml
COPY services/spring-services/dagents-spring-common/src services/spring-services/dagents-spring-common/src
COPY services/spring-services/spring-core-service/src services/spring-services/spring-core-service/src

RUN mvn -q -f services/spring-services/pom.xml -pl spring-core-service -am -DskipTests package

FROM ocaml/opam:debian-12-ocaml-5.4 AS ocaml-builder

WORKDIR /workspace/bindings/ocaml

COPY bindings/ocaml /workspace/bindings/ocaml

RUN opam install -y . --deps-only
RUN /bin/bash -lc "eval \"$(opam env)\" && dune build ./bin/dagentsc.exe"

FROM eclipse-temurin:25-jre-jammy

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=spring-builder /workspace/services/spring-services/spring-core-service/target/spring-core-service-0.1.0-SNAPSHOT.jar /app/app.jar
COPY --from=ocaml-builder /workspace/bindings/ocaml/_build/default/bin/dagentsc.exe /usr/local/bin/dagentsc

ENV DAGENTS_OCAML_MANIFEST_CLI=/usr/local/bin/dagentsc

EXPOSE 8060

ENTRYPOINT ["java", "-jar", "/app/app.jar"]
