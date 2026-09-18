param name string
param location string
param tags object
param enableApplicationInsights bool = false

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${name}'
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
    features: {
      enableLogAccessUsingOnlyResourcePermissions: true
    }
  }
}

resource insights 'Microsoft.Insights/components@2020-02-02' = if (enableApplicationInsights) {
  name: 'appi-${name}'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logs.id
    DisableLocalAuth: true
    RetentionInDays: 30
    IngestionMode: 'LogAnalytics'
  }
}

output workspaceId string = logs.id
output workspaceName string = logs.name
output insightsName string = enableApplicationInsights ? insights!.name : ''
