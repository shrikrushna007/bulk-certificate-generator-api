from __future__ import annotations

from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
    Tags,
)
from aws_cdk import (
    aws_certificatemanager as acm,
)
from aws_cdk import (
    aws_cloudwatch as cloudwatch,
)
from aws_cdk import (
    aws_cloudwatch_actions as cloudwatch_actions,
)
from aws_cdk import (
    aws_ec2 as ec2,
)
from aws_cdk import (
    aws_ecs as ecs,
)
from aws_cdk import (
    aws_elasticache as elasticache,
)
from aws_cdk import (
    aws_elasticloadbalancingv2 as elbv2,
)
from aws_cdk import (
    aws_iam as iam,
)
from aws_cdk import (
    aws_kms as kms,
)
from aws_cdk import (
    aws_logs as logs,
)
from aws_cdk import (
    aws_rds as rds,
)
from aws_cdk import (
    aws_s3 as s3,
)
from aws_cdk import (
    aws_secretsmanager as secretsmanager,
)
from aws_cdk import (
    aws_sns as sns,
)
from constructs import Construct


class CertFlowStack(Stack):
    """Managed AWS deployment for the CertFlow API and background workers."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs: object) -> None:
        super().__init__(scope, construct_id, **kwargs)

        certificate_arn = cdk.CfnParameter(
            self,
            "CertificateArn",
            type="String",
            description="Issued ACM certificate ARN for certificates.shrikrushna.me",
        )

        Tags.of(self).add("Application", "certflow")
        Tags.of(self).add("Environment", "production")
        Tags.of(self).add("ManagedBy", "aws-cdk")

        encryption_key = kms.Key(
            self,
            "DataKey",
            alias="alias/certflow-production",
            description="Encrypts CertFlow data, secrets, and logs",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        encryption_key.add_to_resource_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                principals=[
                    iam.ServicePrincipal(
                        f"logs.{Stack.of(self).region}.{Stack.of(self).url_suffix}"
                    )
                ],
                actions=[
                    "kms:Encrypt",
                    "kms:Decrypt",
                    "kms:ReEncrypt*",
                    "kms:GenerateDataKey*",
                    "kms:DescribeKey",
                ],
                resources=["*"],
                conditions={
                    "ArnLike": {
                        "kms:EncryptionContext:aws:logs:arn": cdk.Fn.sub(
                            "arn:${AWS::Partition}:logs:${AWS::Region}:"
                            "${AWS::AccountId}:log-group:*"
                        )
                    }
                },
            )
        )

        vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=2,
            nat_gateways=0,
            ip_addresses=ec2.IpAddresses.cidr("10.42.0.0/16"),
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                ),
                ec2.SubnetConfiguration(
                    name="data",
                    subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                    cidr_mask=24,
                ),
            ],
        )
        vpc.add_gateway_endpoint(
            "S3Endpoint",
            service=ec2.GatewayVpcEndpointAwsService.S3,
        )

        task_security_group = ec2.SecurityGroup(
            self,
            "TaskSecurityGroup",
            vpc=vpc,
            description="Network access for CertFlow ECS tasks",
            allow_all_outbound=True,
        )
        database_security_group = ec2.SecurityGroup(
            self,
            "DatabaseSecurityGroup",
            vpc=vpc,
            description="PostgreSQL access from CertFlow tasks only",
            allow_all_outbound=False,
        )
        database_security_group.add_ingress_rule(
            task_security_group,
            ec2.Port.tcp(5432),
            "PostgreSQL from ECS tasks",
        )
        cache_security_group = ec2.SecurityGroup(
            self,
            "CacheSecurityGroup",
            vpc=vpc,
            description="Valkey access from CertFlow tasks only",
            allow_all_outbound=False,
        )
        cache_security_group.add_ingress_rule(
            task_security_group,
            ec2.Port.tcp(6379),
            "Valkey from ECS tasks",
        )

        data_subnets = vpc.select_subnets(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED)
        database_subnet_group = rds.CfnDBSubnetGroup(
            self,
            "DatabaseSubnetGroup",
            db_subnet_group_description="Private CertFlow database subnets",
            subnet_ids=data_subnets.subnet_ids,
        )
        database_parameter_group = rds.CfnDBParameterGroup(
            self,
            "DatabaseParameterGroup",
            description="PostgreSQL 18 settings for CertFlow",
            family="postgres18",
            parameters={"rds.force_ssl": "1"},
        )
        database_log_group = logs.LogGroup(
            self,
            "DatabaseLogGroup",
            log_group_name="/aws/rds/instance/certflow-production/postgresql",
            encryption_key=encryption_key,
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.RETAIN,
        )
        database = rds.CfnDBInstance(
            self,
            "Database",
            db_instance_identifier="certflow-production",
            engine="postgres",
            engine_version="18.6",
            db_instance_class="db.t4g.micro",
            db_name="certflow",
            master_username="certflow_owner",
            manage_master_user_password=True,
            master_user_secret=rds.CfnDBInstance.MasterUserSecretProperty(
                kms_key_id=encryption_key.key_arn
            ),
            allocated_storage="20",
            max_allocated_storage=100,
            storage_type="gp3",
            storage_encrypted=True,
            kms_key_id=encryption_key.key_arn,
            multi_az=True,
            publicly_accessible=False,
            db_subnet_group_name=database_subnet_group.ref,
            vpc_security_groups=[database_security_group.security_group_id],
            db_parameter_group_name=database_parameter_group.ref,
            backup_retention_period=1,
            deletion_protection=True,
            enable_performance_insights=True,
            performance_insights_kms_key_id=encryption_key.key_arn,
            performance_insights_retention_period=7,
            enable_cloudwatch_logs_exports=["postgresql"],
            auto_minor_version_upgrade=True,
            copy_tags_to_snapshot=True,
        )
        database.add_resource_dependency(database_subnet_group)
        database.add_resource_dependency(database_parameter_group)
        database.add_resource_dependency(database_log_group.node.default_child)
        database.cfn_options.deletion_policy = cdk.CfnDeletionPolicy.SNAPSHOT
        database.cfn_options.update_replace_policy = cdk.CfnDeletionPolicy.SNAPSHOT

        cache_subnet_group = elasticache.CfnSubnetGroup(
            self,
            "CacheSubnetGroup",
            description="Private CertFlow cache subnets",
            subnet_ids=data_subnets.subnet_ids,
        )
        cache_secret = secretsmanager.Secret(
            self,
            "CacheSecret",
            description="Credentials for the CertFlow Valkey application user",
            encryption_key=encryption_key,
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template='{"username":"certflow-app"}',
                generate_string_key="password",
                exclude_punctuation=True,
                password_length=48,
            ),
        )
        disabled_default_user = elasticache.CfnUser(
            self,
            "DisabledDefaultCacheUser",
            engine="valkey",
            user_id="certflow-default",
            user_name="default",
            access_string="off ~* -@all",
            authentication_mode={
                "Type": "password",
                "Passwords": [
                    cache_secret.secret_value_from_json("password").unsafe_unwrap()
                ],
            },
        )
        application_cache_user = elasticache.CfnUser(
            self,
            "ApplicationCacheUser",
            engine="valkey",
            user_id="certflow-app",
            user_name="certflow-app",
            access_string="on ~* +@all",
            authentication_mode={
                "Type": "password",
                "Passwords": [
                    cache_secret.secret_value_from_json("password").unsafe_unwrap()
                ],
            },
        )
        cache_user_group = elasticache.CfnUserGroup(
            self,
            "CacheUserGroup",
            engine="valkey",
            user_group_id="certflow-production",
            user_ids=[disabled_default_user.ref, application_cache_user.ref],
        )
        cache_parameter_group = elasticache.CfnParameterGroup(
            self,
            "CacheParameterGroup",
            cache_parameter_group_family="valkey9",
            description="Valkey settings for a durable Celery broker",
            properties={"maxmemory-policy": "noeviction"},
        )
        cache_slow_log = logs.LogGroup(
            self,
            "CacheSlowLog",
            log_group_name="/aws/elasticache/certflow-production/slow-log",
            encryption_key=encryption_key,
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.RETAIN,
        )
        cache_engine_log = logs.LogGroup(
            self,
            "CacheEngineLog",
            log_group_name="/aws/elasticache/certflow-production/engine-log",
            encryption_key=encryption_key,
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.RETAIN,
        )
        cache = elasticache.CfnReplicationGroup(
            self,
            "Cache",
            replication_group_id="certflow-production",
            replication_group_description="Multi-AZ Valkey broker for CertFlow",
            engine="valkey",
            engine_version="9.1",
            cache_node_type="cache.t4g.micro",
            cache_parameter_group_name=cache_parameter_group.ref,
            cache_subnet_group_name=cache_subnet_group.ref,
            security_group_ids=[cache_security_group.security_group_id],
            user_group_ids=[cache_user_group.ref],
            num_node_groups=1,
            replicas_per_node_group=1,
            automatic_failover_enabled=True,
            multi_az_enabled=True,
            transit_encryption_enabled=True,
            at_rest_encryption_enabled=True,
            kms_key_id=encryption_key.key_arn,
            snapshot_retention_limit=7,
            snapshot_window="18:00-19:00",
            preferred_maintenance_window="sun:19:30-sun:20:30",
            auto_minor_version_upgrade=True,
            log_delivery_configurations=[
                elasticache.CfnReplicationGroup.LogDeliveryConfigurationRequestProperty(
                    destination_type="cloudwatch-logs",
                    destination_details=elasticache.CfnReplicationGroup.DestinationDetailsProperty(
                        cloud_watch_logs_details=elasticache.CfnReplicationGroup.CloudWatchLogsDestinationDetailsProperty(
                            log_group=cache_slow_log.log_group_name
                        )
                    ),
                    log_format="json",
                    log_type="slow-log",
                ),
                elasticache.CfnReplicationGroup.LogDeliveryConfigurationRequestProperty(
                    destination_type="cloudwatch-logs",
                    destination_details=elasticache.CfnReplicationGroup.DestinationDetailsProperty(
                        cloud_watch_logs_details=elasticache.CfnReplicationGroup.CloudWatchLogsDestinationDetailsProperty(
                            log_group=cache_engine_log.log_group_name
                        )
                    ),
                    log_format="json",
                    log_type="engine-log",
                ),
            ],
        )
        cache.add_resource_dependency(cache_subnet_group)
        cache.add_resource_dependency(cache_user_group)
        cache.add_resource_dependency(cache_parameter_group)

        access_log_bucket = s3.Bucket(
            self,
            "AccessLogs",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            object_ownership=s3.ObjectOwnership.OBJECT_WRITER,
            lifecycle_rules=[
                s3.LifecycleRule(expiration=Duration.days(90)),
            ],
            removal_policy=RemovalPolicy.RETAIN,
        )
        certificate_bucket = s3.Bucket(
            self,
            "Certificates",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=encryption_key,
            bucket_key_enabled=True,
            versioned=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            server_access_logs_bucket=access_log_bucket,
            server_access_logs_prefix="s3/",
            lifecycle_rules=[
                s3.LifecycleRule(
                    abort_incomplete_multipart_upload_after=Duration.days(7),
                    noncurrent_version_expiration=Duration.days(30),
                )
            ],
            removal_policy=RemovalPolicy.RETAIN,
        )

        cluster = ecs.Cluster(
            self,
            "Cluster",
            vpc=vpc,
            cluster_name="certflow-production",
            container_insights_v2=ecs.ContainerInsights.ENABLED,
        )
        image = ecs.ContainerImage.from_asset(str(Path(__file__).resolve().parents[1]))
        database_secret = secretsmanager.Secret.from_secret_complete_arn(
            self,
            "DatabaseSecret",
            database.attr_master_user_secret_secret_arn,
        )

        common_environment = {
            "APP_ENV": "production",
            "PROCESSING_MODE": "distributed",
            "STORAGE_BACKEND": "s3",
            "S3_BUCKET": certificate_bucket.bucket_name,
            "S3_REGION": self.region,
            "S3_AUTO_CREATE_BUCKET": "false",
            "DATABASE_HOST": database.attr_endpoint_address,
            "DATABASE_PORT": database.attr_endpoint_port,
            "DATABASE_NAME": "certflow",
            "DATABASE_SSL_MODE": "require",
            "CELERY_BROKER_HOST": cache.attr_primary_end_point_address,
            "CELERY_BROKER_PORT": cache.attr_primary_end_point_port,
            "CELERY_BROKER_USERNAME": "certflow-app",
            "CELERY_BROKER_TLS": "true",
            "MAX_RECIPIENTS": "500",
            "MAX_CSV_RECIPIENTS": "0",
            "PROCESSING_BATCH_SIZE": "100",
            "DISTRIBUTED_JOB_PARALLELISM": "16",
            "RENDER_WORKERS": "2",
            "CLAIM_TIMEOUT_SECONDS": "300",
            "LOG_LEVEL": "INFO",
            "LOG_FORMAT": "json",
        }
        common_secrets = {
            "DATABASE_USER": ecs.Secret.from_secrets_manager(database_secret, "username"),
            "DATABASE_PASSWORD": ecs.Secret.from_secrets_manager(database_secret, "password"),
            "CELERY_BROKER_PASSWORD": ecs.Secret.from_secrets_manager(cache_secret, "password"),
        }

        api_log_group = self._log_group("ApiLogs", "/ecs/certflow/api", encryption_key)
        worker_log_group = self._log_group("WorkerLogs", "/ecs/certflow/worker", encryption_key)
        beat_log_group = self._log_group("BeatLogs", "/ecs/certflow/beat", encryption_key)

        api_task, api_container = self._task_definition(
            "Api",
            cpu=512,
            memory_mib=1024,
            image=image,
            log_group=api_log_group,
            environment=common_environment,
            secrets=common_secrets,
        )
        api_container.add_port_mappings(ecs.PortMapping(container_port=8000))
        worker_task, _ = self._task_definition(
            "Worker",
            cpu=1024,
            memory_mib=2048,
            image=image,
            log_group=worker_log_group,
            environment=common_environment,
            secrets=common_secrets,
            command=[
                "celery",
                "-A",
                "app.celery_app:celery_app",
                "worker",
                "--loglevel=INFO",
                "--concurrency=2",
            ],
        )
        beat_task, _ = self._task_definition(
            "Beat",
            cpu=256,
            memory_mib=512,
            image=image,
            log_group=beat_log_group,
            environment=common_environment,
            secrets=common_secrets,
            command=[
                "celery",
                "-A",
                "app.celery_app:celery_app",
                "beat",
                "--loglevel=INFO",
                "--schedule=/tmp/celerybeat-schedule",
            ],
        )

        for task in (api_task, worker_task, beat_task):
            certificate_bucket.grant_read_write(task.task_role)
            database_secret.grant_read(task.execution_role)
            cache_secret.grant_read(task.execution_role)

        api_service = ecs.FargateService(
            self,
            "ApiService",
            cluster=cluster,
            task_definition=api_task,
            desired_count=2,
            assign_public_ip=True,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            security_groups=[task_security_group],
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
            health_check_grace_period=Duration.minutes(5),
            min_healthy_percent=50,
            max_healthy_percent=100,
            enable_execute_command=False,
        )
        worker_service = ecs.FargateService(
            self,
            "WorkerService",
            cluster=cluster,
            task_definition=worker_task,
            desired_count=2,
            assign_public_ip=True,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            security_groups=[task_security_group],
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
            min_healthy_percent=50,
            max_healthy_percent=100,
            enable_execute_command=False,
        )
        ecs.FargateService(
            self,
            "BeatService",
            cluster=cluster,
            task_definition=beat_task,
            desired_count=1,
            assign_public_ip=True,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            security_groups=[task_security_group],
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
            min_healthy_percent=0,
            max_healthy_percent=100,
            enable_execute_command=False,
        )

        api_scaling = api_service.auto_scale_task_count(min_capacity=2, max_capacity=3)
        api_scaling.scale_on_cpu_utilization(
            "ApiCpuScaling",
            target_utilization_percent=60,
            scale_in_cooldown=Duration.minutes(5),
            scale_out_cooldown=Duration.minutes(1),
        )
        worker_scaling = worker_service.auto_scale_task_count(min_capacity=2, max_capacity=3)
        worker_scaling.scale_on_cpu_utilization(
            "WorkerCpuScaling",
            target_utilization_percent=65,
            scale_in_cooldown=Duration.minutes(10),
            scale_out_cooldown=Duration.minutes(1),
        )

        load_balancer = elbv2.ApplicationLoadBalancer(
            self,
            "LoadBalancer",
            vpc=vpc,
            internet_facing=True,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            idle_timeout=Duration.seconds(60),
        )
        load_balancer.log_access_logs(access_log_bucket, "alb")
        task_security_group.add_ingress_rule(
            load_balancer.connections.security_groups[0],
            ec2.Port.tcp(8000),
            "API traffic from the load balancer",
        )
        load_balancer.add_redirect(
            source_port=80,
            source_protocol=elbv2.ApplicationProtocol.HTTP,
            target_port=443,
            target_protocol=elbv2.ApplicationProtocol.HTTPS,
        )
        certificate = acm.Certificate.from_certificate_arn(
            self,
            "Certificate",
            certificate_arn.value_as_string,
        )
        listener = load_balancer.add_listener(
            "HttpsListener",
            port=443,
            protocol=elbv2.ApplicationProtocol.HTTPS,
            certificates=[certificate],
            ssl_policy=elbv2.SslPolicy.RECOMMENDED_TLS,
        )
        listener.add_targets(
            "ApiTargets",
            port=8000,
            protocol=elbv2.ApplicationProtocol.HTTP,
            targets=[api_service],
            health_check=elbv2.HealthCheck(
                path="/health",
                healthy_http_codes="200",
                interval=Duration.seconds(30),
                timeout=Duration.seconds(5),
            ),
            deregistration_delay=Duration.seconds(30),
        )

        alarm_topic = sns.Topic(
            self,
            "AlarmTopic",
            display_name="CertFlow production alarms",
            master_key=encryption_key,
        )
        database_alarm = cloudwatch.Alarm(
            self,
            "DatabaseCpuAlarm",
            metric=cloudwatch.Metric(
                namespace="AWS/RDS",
                metric_name="CPUUtilization",
                dimensions_map={"DBInstanceIdentifier": database.ref},
                statistic="Average",
                period=Duration.minutes(5),
            ),
            threshold=80,
            evaluation_periods=3,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        )
        cache_alarm = cloudwatch.Alarm(
            self,
            "CacheCpuAlarm",
            metric=cloudwatch.Metric(
                namespace="AWS/ElastiCache",
                metric_name="EngineCPUUtilization",
                dimensions_map={"ReplicationGroupId": cache.ref},
                statistic="Average",
                period=Duration.minutes(5),
            ),
            threshold=75,
            evaluation_periods=3,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        )
        database_alarm.add_alarm_action(cloudwatch_actions.SnsAction(alarm_topic))
        cache_alarm.add_alarm_action(cloudwatch_actions.SnsAction(alarm_topic))

        cdk.CfnOutput(self, "ApplicationUrl", value="https://certificates.shrikrushna.me")
        cdk.CfnOutput(self, "LoadBalancerDnsName", value=load_balancer.load_balancer_dns_name)
        cdk.CfnOutput(self, "CertificateBucketName", value=certificate_bucket.bucket_name)
        cdk.CfnOutput(self, "AlarmTopicArn", value=alarm_topic.topic_arn)
        cdk.CfnOutput(self, "ClusterName", value=cluster.cluster_name)

    def _log_group(self, construct_id: str, name: str, key: kms.IKey) -> logs.LogGroup:
        return logs.LogGroup(
            self,
            construct_id,
            log_group_name=name,
            encryption_key=key,
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.RETAIN,
        )

    def _task_definition(
        self,
        prefix: str,
        *,
        cpu: int,
        memory_mib: int,
        image: ecs.ContainerImage,
        log_group: logs.ILogGroup,
        environment: dict[str, str],
        secrets: dict[str, ecs.Secret],
        command: list[str] | None = None,
    ) -> tuple[ecs.FargateTaskDefinition, ecs.ContainerDefinition]:
        task = ecs.FargateTaskDefinition(
            self,
            f"{prefix}Task",
            cpu=cpu,
            memory_limit_mib=memory_mib,
            runtime_platform=ecs.RuntimePlatform(
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
                cpu_architecture=ecs.CpuArchitecture.X86_64,
            ),
        )
        container = task.add_container(
            f"{prefix}Container",
            image=image,
            environment=environment,
            secrets=secrets,
            command=command,
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix=prefix.lower(),
                log_group=log_group,
            ),
            readonly_root_filesystem=False,
        )
        return task, container
