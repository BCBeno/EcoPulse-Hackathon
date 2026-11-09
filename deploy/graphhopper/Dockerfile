FROM eclipse-temurin:21-jre

ARG GH_VERSION=11.1
ADD https://repo1.maven.org/maven2/com/graphhopper/graphhopper-web/${GH_VERSION}/graphhopper-web-${GH_VERSION}.jar /app/graphhopper.jar
COPY config.yml /app/config.yml

WORKDIR /app
EXPOSE 8989
# The OSM extract (iasi.osm.pbf) and the graph cache live in the /data volume.
# Delete /data/graph-cache after replacing the extract so the graph is rebuilt.
ENTRYPOINT ["sh", "-c", "exec java $JAVA_OPTS -jar /app/graphhopper.jar server /app/config.yml"]
