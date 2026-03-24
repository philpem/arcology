pipeline {
    agent any

    environment {
        // Test database credentials — used only for migration testing
        POSTGRES_DB       = 'arcology_test'
        POSTGRES_USER     = 'arcology_test'
        POSTGRES_PASSWORD = 'arcology_test'
        SECRET_KEY        = 'jenkins-ci-test-key-not-for-production'
        WORKER_API_KEY    = 'ci-test-worker-key'
        // BuildKit required for --mount=type=cache in worker Dockerfile
        DOCKER_BUILDKIT   = '1'
    }

    stages {
        stage('Static Checks') {
            steps {
                sh 'python3 ci/check_syntax.py'
                sh 'python3 ci/check_migration_sanity.py'
                sh '''
                    python3 -m venv .venv-ci
                    . .venv-ci/bin/activate
                    pip install -q -r requirements.txt
                    python -m pytest ci/test_slug.py --junit-xml=test-results/static.xml -v
                '''
            }
        }

        stage('Application Tests (SQLite)') {
            steps {
                withEnv(["SQLALCHEMY_DATABASE_URI=sqlite:///:memory:"]) {
                    sh '''
                        python3 -m venv .venv-ci
                        . .venv-ci/bin/activate
                        pip install -q -r requirements.txt

                        echo "=== Checking imports ==="
                        python ci/check_imports.py

                        echo "=== Running application tests ==="
                        python -m pytest ci/ --junit-xml=test-results/app.xml -v
                    '''
                }
            }
        }

        stage('Migration Tests') {
            steps {
                script {
                    // Start PostgreSQL container
                    docker.image('postgres:16').withRun(
                        "-e POSTGRES_DB=${POSTGRES_DB} " +
                        "-e POSTGRES_USER=${POSTGRES_USER} " +
                        "-e POSTGRES_PASSWORD=${POSTGRES_PASSWORD} " +
                        "-p 0:5432"
                    ) { pg ->
                        // Wait for PostgreSQL to be ready
                        sh """
                            for i in \$(seq 1 30); do
                                if docker exec ${pg.id} pg_isready -U ${POSTGRES_USER}; then
                                    break
                                fi
                                sleep 1
                            done
                        """

                        // Discover the dynamically assigned host port
                        def pgPort = sh(
                            script: "docker port ${pg.id} 5432 | head -1 | cut -d: -f2",
                            returnStdout: true
                        ).trim()

                        // Set up virtualenv and run migration tests
                        withEnv([
                            "SQLALCHEMY_DATABASE_URI=postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@localhost:${pgPort}/${POSTGRES_DB}"
                        ]) {
                            sh '''
                                python3 -m venv .venv-ci
                                . .venv-ci/bin/activate
                                pip install -q -r requirements.txt

                                echo "=== Upgrading database (fresh) ==="
                                flask db upgrade

                                echo "=== Downgrading database (full) ==="
                                flask db downgrade base

                                echo "=== Re-upgrading database (idempotency check) ==="
                                flask db upgrade
                            '''
                        }
                    }
                }
            }
        }

        stage('Docker Build') {
            steps {
                sh 'docker build -t arcology-web:test .'
                sh 'docker build -t arcology-worker:test -f worker/Dockerfile .'
            }
        }
    }

    post {
        always {
            junit allowEmptyResults: true, testResults: 'test-results/**/*.xml'
            // Clean up virtualenv, test results, and test Docker images
            sh 'rm -rf .venv-ci test-results || true'
            sh 'docker rmi arcology-web:test arcology-worker:test 2>/dev/null || true'
        }
    }
}
