FROM maven:3.9.11-eclipse-temurin-25 AS builder

WORKDIR /workspace

COPY services/spring-services/pom.xml services/spring-services/pom.xml
COPY services/spring-services/dagents-spring-common/pom.xml services/spring-services/dagents-spring-common/pom.xml
COPY services/spring-services/spring-control-service/pom.xml services/spring-services/spring-control-service/pom.xml
COPY services/spring-services/dagents-spring-common/src services/spring-services/dagents-spring-common/src
COPY services/spring-services/spring-control-service/src services/spring-services/spring-control-service/src

RUN mvn -q -f services/spring-services/pom.xml -pl spring-control-service -am -DskipTests package

FROM eclipse-temurin:25-jre-jammy

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /workspace/services/spring-services/spring-control-service/target/spring-control-service-0.1.0-SNAPSHOT.jar /app/app.jar

EXPOSE 8050

ENTRYPOINT ["java", "-jar", "/app/app.jar"]
