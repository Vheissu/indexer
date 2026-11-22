# Liquibase 5 no longer bundles JDBC drivers, so add the PostgreSQL driver at a pinned
# version, verified against its SHA-256 (the SHA-1 matches the one Maven Central publishes).
FROM liquibase/liquibase:5.0.4
ADD --chmod=644 --checksum=sha256:73914527305a40cce504b0d3d90b23caf565136912d607ae8ae7c5895512332c \
    https://repo1.maven.org/maven2/org/postgresql/postgresql/42.7.14/postgresql-42.7.14.jar \
    /liquibase/lib/postgresql-42.7.14.jar
