param name string
param tags object
param endpoint string
param identityId string
param identityClientId string

resource bot 'Microsoft.BotService/botServices@2022-09-15' = {
  name: name
  location: 'global'
  tags: tags
  kind: 'azurebot'
  sku: {
    name: 'F0'
  }
  properties: {
    displayName: name
    endpoint: endpoint
    msaAppType: 'UserAssignedMSI'
    msaAppId: identityClientId
    msaAppMSIResourceId: identityId
    msaAppTenantId: tenant().tenantId
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: true
  }
}

resource teams 'Microsoft.BotService/botServices/channels@2022-09-15' = {
  parent: bot
  name: 'MsTeamsChannel'
  location: 'global'
  properties: {
    channelName: 'MsTeamsChannel'
    properties: {
      isEnabled: true
    }
  }
}

output id string = bot.id
