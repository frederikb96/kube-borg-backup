"""Hook system resource parsing utilities."""

from typing import Any
from concurrent.futures import ThreadPoolExecutor, as_completed
from kubernetes import client
from kubernetes.stream import stream


def parse_resource(resource_string: str) -> tuple[str, str, str]:
    """Parse Kubernetes resource string into API components.

    Converts simple resource syntax like "deployment/nginx" into the tuple
    (api_version, kind, name) required for Kubernetes API calls.

    Supported resource types:
    - deployment (apps/v1, Deployment)
    - statefulset (apps/v1, StatefulSet)
    - replicaset (apps/v1, ReplicaSet)
    - daemonset (apps/v1, DaemonSet)
    - pod (v1, Pod)
    - service (v1, Service)

    Args:
        resource_string: Resource in format "type/name" (e.g., "deployment/nginx")
                        Type is case-insensitive and accepts singular/plural forms

    Returns:
        Tuple of (api_version, kind, name)
        Example: ("apps/v1", "Deployment", "nginx")

    Raises:
        ValueError: If format is invalid or resource type is unknown

    Examples:
        >>> parse_resource("deployment/nginx")
        ('apps/v1', 'Deployment', 'nginx')

        >>> parse_resource("statefulset/postgres")
        ('apps/v1', 'StatefulSet', 'postgres')

        >>> parse_resource("pod/test-pod")
        ('v1', 'Pod', 'test-pod')
    """
    # Validate input format
    if not resource_string or not isinstance(resource_string, str):
        raise ValueError("Resource string must be a non-empty string")

    # Strip whitespace
    resource_string = resource_string.strip()

    # Split on slash - expect exactly 2 parts
    parts = resource_string.split('/')
    if len(parts) != 2:
        raise ValueError(
            f"Invalid resource format: '{resource_string}'\n"
            f"Expected format: 'type/name' (e.g., 'deployment/nginx')"
        )

    resource_type, resource_name = parts

    # Validate both parts are non-empty
    if not resource_type or not resource_name:
        raise ValueError(
            f"Invalid resource format: '{resource_string}'\n"
            f"Both type and name must be non-empty"
        )

    # Normalize to lowercase for matching
    resource_type = resource_type.lower()

    # Mapping: resource type -> (api_version, kind)
    # Support both singular and plural forms
    type_mappings = {
        'deployment': ('apps/v1', 'Deployment'),
        'deployments': ('apps/v1', 'Deployment'),
        'statefulset': ('apps/v1', 'StatefulSet'),
        'statefulsets': ('apps/v1', 'StatefulSet'),
        'replicaset': ('apps/v1', 'ReplicaSet'),
        'replicasets': ('apps/v1', 'ReplicaSet'),
        'daemonset': ('apps/v1', 'DaemonSet'),
        'daemonsets': ('apps/v1', 'DaemonSet'),
        'pod': ('v1', 'Pod'),
        'pods': ('v1', 'Pod'),
        'service': ('v1', 'Service'),
        'services': ('v1', 'Service'),
    }

    # Look up resource type
    if resource_type not in type_mappings:
        supported = sorted(set(k for k in type_mappings.keys() if not k.endswith('s')))
        raise ValueError(
            f"Unknown resource type: '{resource_type}'\n"
            f"Supported types: {', '.join(supported)}"
        )

    api_version, kind = type_mappings[resource_type]

    return (api_version, kind, resource_name)


def execute_exec_hook(
    api_client: client.ApiClient,
    namespace: str,
    resource_string: str,
    command: list[str],
    container: str | None = None
) -> dict[str, str]:
    """Execute command in pod via Kubernetes exec API.

    Args:
        api_client: Kubernetes API client
        namespace: Namespace of the pod
        resource_string: Resource in format "pod/name"
        command: Command to execute as list (e.g., ["echo", "test"])
        container: Optional container name (for multi-container pods)

    Returns:
        Dict with 'stdout' and 'stderr' keys

    Raises:
        ValueError: If resource type is not 'pod'
        Exception: If command returns non-zero exit code

    Examples:
        >>> from kubernetes import client, config
        >>> config.load_kube_config()
        >>> k8s_client = client.ApiClient()
        >>> result = execute_exec_hook(k8s_client, "default", "pod/nginx", ["echo", "test"])
        >>> print(result['stdout'])
        test
    """
    # Parse resource string
    api_version, kind, pod_name = parse_resource(resource_string)

    # Validate resource type is pod
    if kind != 'Pod':
        raise ValueError(
            f"Exec hooks only support pods, got: {kind}\n"
            f"Resource string: '{resource_string}'"
        )

    # Create CoreV1Api instance
    v1 = client.CoreV1Api(api_client)

    # Build exec parameters
    exec_kwargs: dict[str, Any] = {
        'name': pod_name,
        'namespace': namespace,
        'command': command,
        'stderr': True,
        'stdout': True,
        'stdin': False,
        'tty': False,
        '_preload_content': False
    }

    # Add container if specified
    if container:
        exec_kwargs['container'] = container

    # Execute command via stream API
    try:
        resp = stream(
            v1.connect_get_namespaced_pod_exec,
            **exec_kwargs
        )
    except Exception as e:
        raise Exception(
            f"Failed to execute command in pod '{pod_name}' in namespace '{namespace}': {e}"
        ) from e

    # Read output
    stdout_output = ''
    stderr_output = ''

    # Read all available output
    while resp.is_open():
        resp.update(timeout=1)
        if resp.peek_stdout():
            stdout_output += resp.read_stdout()
        if resp.peek_stderr():
            stderr_output += resp.read_stderr()

    # Get exit code
    exit_code = resp.returncode

    # Check exit code
    if exit_code != 0:
        raise Exception(
            f"Command failed with exit code {exit_code}\n"
            f"Pod: {pod_name}, Namespace: {namespace}\n"
            f"Command: {' '.join(command)}\n"
            f"Stdout: {stdout_output}\n"
            f"Stderr: {stderr_output}"
        )

    return {
        'stdout': stdout_output,
        'stderr': stderr_output
    }


def execute_scale_hook(
    api_client: client.ApiClient,
    namespace: str,
    resource_string: str,
    replicas: int
) -> int:
    """Scale deployment or statefulset to specified replica count.

    Args:
        api_client: Kubernetes API client
        namespace: Namespace of the resource
        resource_string: Resource in format "deployment/name" or "statefulset/name"
        replicas: Target replica count

    Returns:
        Current replica count after scaling

    Raises:
        ValueError: If resource type is not deployment or statefulset
        Exception: If patch operation fails

    Examples:
        >>> from kubernetes import client, config
        >>> config.load_kube_config()
        >>> k8s_client = client.ApiClient()
        >>> current = execute_scale_hook(k8s_client, "default", "deployment/nginx", 0)
        >>> print(f"Scaled to {current} replicas")
        Scaled to 0 replicas
    """
    # Parse resource string
    api_version, kind, name = parse_resource(resource_string)

    # Validate resource type is Deployment or StatefulSet
    if kind not in ['Deployment', 'StatefulSet']:
        raise ValueError(
            f"Scale hooks only support Deployment and StatefulSet, got: {kind}\n"
            f"Resource string: '{resource_string}'"
        )

    # Create AppsV1Api instance
    apps_v1 = client.AppsV1Api(api_client)

    # Build patch body
    patch_body = {"spec": {"replicas": replicas}}

    # Patch resource based on kind
    try:
        if kind == 'Deployment':
            result = apps_v1.patch_namespaced_deployment(
                name=name,
                namespace=namespace,
                body=patch_body
            )
        else:  # StatefulSet
            result = apps_v1.patch_namespaced_stateful_set(
                name=name,
                namespace=namespace,
                body=patch_body
            )
    except Exception as e:
        raise Exception(
            f"Failed to scale {kind} '{name}' in namespace '{namespace}' to {replicas} replicas: {e}"
        ) from e

    # Return current replica count from patched object
    return result.spec.replicas


def execute_hooks(
    api_client: client.ApiClient,
    namespace: str,
    hooks: list[dict[str, Any]],
    mode: str = "pre"
) -> dict[str, Any]:
    """Execute list of hooks with parallel execution support.

    Groups consecutive hooks marked with parallel=true and executes them concurrently
    using ThreadPoolExecutor. Non-parallel hooks are executed sequentially.

    Args:
        api_client: Kubernetes API client
        namespace: Namespace for hook execution
        hooks: List of hook configurations, each with:
            - type: "exec" or "scale"
            - parallel: bool (optional, default False)
            - For exec: pod, command, container (optional)
            - For scale: deployment/statefulset, replicas
        mode: "pre" (fail-fast) or "post" (best-effort)
            - pre mode: Abort on first failure and raise exception
            - post mode: Continue on failures, collect errors, no exception

    Returns:
        Dict with execution summary:
            - success: bool (True if all hooks succeeded)
            - executed: int (number of hooks executed)
            - failed: list[str] (error messages from failed hooks)
            - results: list (individual hook results)

    Raises:
        Exception: If mode="pre" and any hook fails

    Examples:
        >>> from kubernetes import client, config
        >>> config.load_kube_config()
        >>> k8s_client = client.ApiClient()
        >>>
        >>> # Sequential hooks
        >>> hooks = [
        ...     {'type': 'scale', 'deployment': 'nginx', 'replicas': 0},
        ...     {'type': 'scale', 'deployment': 'app', 'replicas': 0}
        ... ]
        >>> result = execute_hooks(k8s_client, "default", hooks, mode="pre")
        >>>
        >>> # Parallel hooks (executed concurrently)
        >>> hooks = [
        ...     {'type': 'scale', 'deployment': 'nginx', 'replicas': 0, 'parallel': True},
        ...     {'type': 'scale', 'deployment': 'app', 'replicas': 0, 'parallel': True}
        ... ]
        >>> result = execute_hooks(k8s_client, "default", hooks, mode="pre")
    """
    # Validate mode parameter
    if mode not in ['pre', 'post']:
        raise ValueError(f"Invalid mode: '{mode}'. Expected 'pre' or 'post'")

    # Initialize result tracking
    executed = 0
    failed_messages: list[str] = []
    results: list[Any] = []

    # Group hooks into batches (parallel groups and sequential individual hooks)
    batches = _group_hooks(hooks)

    # Execute each batch
    for batch_type, batch_hooks in batches:
        if batch_type == 'sequential':
            # Execute single hook sequentially
            for hook in batch_hooks:
                try:
                    result = _execute_single_hook(api_client, namespace, hook)
                    results.append(result)
                    executed += 1
                except Exception as e:
                    error_msg = str(e)
                    failed_messages.append(error_msg)

                    # Handle error based on mode
                    if mode == 'pre':
                        # Fail-fast: raise immediately
                        raise Exception(
                            f"Pre-hook failed, aborting execution\n"
                            f"Hook: {hook}\n"
                            f"Error: {error_msg}"
                        ) from e
                    else:
                        # Best-effort: log warning and continue
                        print(f"Warning: Post-hook failed (continuing): {error_msg}", flush=True)
                        executed += 1

        elif batch_type == 'parallel':
            # Execute batch of hooks in parallel using ThreadPoolExecutor
            try:
                with ThreadPoolExecutor(max_workers=len(batch_hooks)) as executor:
                    # Submit all hooks to executor
                    future_to_hook = {
                        executor.submit(_execute_single_hook, api_client, namespace, hook): hook
                        for hook in batch_hooks
                    }

                    # Wait for completion and collect results
                    for future in as_completed(future_to_hook):
                        hook = future_to_hook[future]
                        try:
                            result = future.result()
                            results.append(result)
                            executed += 1
                        except Exception as e:
                            error_msg = str(e)
                            failed_messages.append(error_msg)

                            # Handle error based on mode
                            if mode == 'pre':
                                # Fail-fast: raise immediately
                                raise Exception(
                                    f"Pre-hook failed in parallel batch, aborting execution\n"
                                    f"Hook: {hook}\n"
                                    f"Error: {error_msg}"
                                ) from e
                            else:
                                # Best-effort: log warning and continue
                                print(f"Warning: Post-hook failed (continuing): {error_msg}", flush=True)
                                executed += 1

            except Exception:
                # If this is a pre-hook parallel batch and we got an exception, re-raise
                if mode == 'pre':
                    raise
                # For post-hooks, exception was already logged, continue

    # Build result summary
    success = len(failed_messages) == 0
    return {
        'success': success,
        'executed': executed,
        'failed': failed_messages,
        'results': results
    }


def _group_hooks(hooks: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    """Group consecutive parallel hooks, keep sequential hooks separate.

    Args:
        hooks: List of hook configurations

    Returns:
        List of tuples: (batch_type, hooks_list)
        - batch_type: 'parallel' or 'sequential'
        - hooks_list: List of hooks in this batch

    Examples:
        >>> hooks = [
        ...     {'type': 'scale', 'deployment': 'a', 'replicas': 0},
        ...     {'type': 'scale', 'deployment': 'b', 'replicas': 0, 'parallel': True},
        ...     {'type': 'scale', 'deployment': 'c', 'replicas': 0, 'parallel': True},
        ...     {'type': 'scale', 'deployment': 'd', 'replicas': 0}
        ... ]
        >>> batches = _group_hooks(hooks)
        >>> # Returns: [
        >>> #   ('sequential', [hook_a]),
        >>> #   ('parallel', [hook_b, hook_c]),
        >>> #   ('sequential', [hook_d])
        >>> # ]
    """
    batches: list[tuple[str, list[dict[str, Any]]]] = []
    current_batch: list[dict[str, Any]] = []

    for hook in hooks:
        if hook.get('parallel', False):
            # This is a parallel hook - add to current batch
            current_batch.append(hook)
        else:
            # This is a sequential hook
            # First, flush any accumulated parallel hooks
            if current_batch:
                batches.append(('parallel', current_batch))
                current_batch = []

            # Add this sequential hook as its own batch
            batches.append(('sequential', [hook]))

    # Flush any remaining parallel hooks
    if current_batch:
        batches.append(('parallel', current_batch))

    return batches


def _execute_single_hook(
    api_client: client.ApiClient,
    namespace: str,
    hook: dict[str, Any]
) -> Any:
    """Execute single hook based on type.

    Args:
        api_client: Kubernetes API client
        namespace: Namespace for hook execution
        hook: Hook configuration dict

    Returns:
        Result from hook execution (type depends on hook type)

    Raises:
        ValueError: If hook type is unknown
        Exception: If hook execution fails

    Examples:
        >>> from kubernetes import client, config
        >>> config.load_kube_config()
        >>> k8s_client = client.ApiClient()
        >>>
        >>> # Execute scale hook
        >>> hook = {'type': 'scale', 'deployment': 'nginx', 'replicas': 0}
        >>> result = _execute_single_hook(k8s_client, "default", hook)
        >>>
        >>> # Execute exec hook
        >>> hook = {'type': 'exec', 'pod': 'postgres-0', 'command': ['echo', 'test']}
        >>> result = _execute_single_hook(k8s_client, "default", hook)
    """
    hook_type = hook.get('type')

    if hook_type == 'exec':
        # Build resource string
        resource_string = f"pod/{hook['pod']}"

        # Execute exec hook
        return execute_exec_hook(
            api_client,
            namespace,
            resource_string,
            hook['command'],
            hook.get('container')
        )

    elif hook_type == 'scale':
        # Determine resource type and build resource string
        if 'deployment' in hook:
            resource_string = f"deployment/{hook['deployment']}"
        elif 'statefulset' in hook:
            resource_string = f"statefulset/{hook['statefulset']}"
        else:
            raise ValueError(
                f"Scale hook missing 'deployment' or 'statefulset' field: {hook}"
            )

        # Execute scale hook
        return execute_scale_hook(
            api_client,
            namespace,
            resource_string,
            hook['replicas']
        )

    else:
        raise ValueError(f"Unknown hook type: '{hook_type}' in hook: {hook}")
