param name string
param location string
param tags object
param environmentId string
param identityId string
param registryServer string
@description('Repository:tag or repository@sha256:digest in the new experiment registry.')
param image string
param storageAccountName string
param settings object
@secure()
param secretSettings object
@allowed([0, 1])
param minReplicas int
@allowed(['quarter', 'half', 'one', 'two'])
param resourceProfile string
@minValue(1)
@maxValue(1000)
param maxInstances int
@minValue(1)
param workerParallelism int

var resourceProfiles = {
  quarter: { cpu: json('0.25'), memory: '0.5Gi' }
  half: { cpu: json('0.5'), memory: '1Gi' }
  one: { cpu: 1, memory: '2Gi' }
  two: { cpu: 2, memory: '4Gi' }
}

// The protected settings win; old Azure keys are not copied in Entra mode.
var allowedSecretNames = ['AZURE_OPENAI_API_KEY', 'AZURE_SEARCH_API_KEY', 'GITHUB_TOKEN', 'TAVILY_API_KEY', 'BRAVE_API_KEY']
var acceptedSecrets = filter(items(secretSettings), item => contains(allowedSecretNames, item.key) && (item.key != 'AZURE_OPENAI_API_KEY' || settings.AZURE_OPENAI_AUTH_MODE == 'key') && (item.key != 'AZURE_SEARCH_API_KEY' || settings.AZURE_SEARCH_AUTH_MODE == 'key'))
var protectedSettings = union(settings, {
  BENCHMARK_TARGET_ID: resourceId('Microsoft.App/containerApps', name)
})
var settingsEnv = [for setting in items(protectedSettings): {
  name: setting.key
  value: string(setting.value)
}]
var secretEnv = [for (setting, index) in acceptedSecrets: {
  name: setting.key
  secretRef: 'setting-${index}'
}]

resource app 'Microsoft.App/containerApps@2025-01-01' = {
  name: name
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identityId}': {}
    }
  }
  properties: {
    environmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        allowInsecure: false
        targetPort: 3978
        transport: 'http'
      }
      registries: [
        {
          server: registryServer
          identity: identityId
        }
      ]
      secrets: [for (setting, index) in acceptedSecrets: {
        name: 'setting-${index}'
        // Indexed access to the secure object is not tracked by the linter.
#disable-next-line use-secure-value-for-secure-inputs
        value: secretSettings[setting.key]
      }]
    }
    template: {
      containers: [
        {
          name: 'advisor'
          image: '${registryServer}/${image}'
          resources: resourceProfiles[resourceProfile]
          env: concat(settingsEnv, secretEnv)
          probes: [
            {
              type: 'Startup'
              httpGet: {
                path: '/healthz'
                port: 3978
                scheme: 'HTTP'
              }
              periodSeconds: 5
              failureThreshold: 60
              timeoutSeconds: 3
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/healthz'
                port: 3978
                scheme: 'HTTP'
              }
              periodSeconds: 10
              failureThreshold: 3
              timeoutSeconds: 3
            }
            {
              type: 'Liveness'
              httpGet: {
                path: '/healthz'
                port: 3978
                scheme: 'HTTP'
              }
              periodSeconds: 30
              failureThreshold: 3
              timeoutSeconds: 3
            }
          ]
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxInstances
        rules: [
          {
            name: 'http'
            http: {
              metadata: {
                concurrentRequests: string(workerParallelism)
              }
            }
          }
          {
            name: 'advisor-queue'
            custom: {
              type: 'azure-queue'
              identity: identityId
              metadata: {
                accountName: storageAccountName
                queueName: 'advisor-incoming'
                queueLength: string(workerParallelism)
                activationQueueLength: '0'
              }
            }
          }
        ]
      }
    }
  }
}

output id string = app.id
output endpoint string = 'https://${app.properties.configuration.ingress!.fqdn}'
