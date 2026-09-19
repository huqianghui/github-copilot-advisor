targetScope = 'resourceGroup'

@allowed(['southeastasia'])
param location string = 'southeastasia'
param perimeterName string
param ownerRunId string
@description('Receipt-owned ACA Storage account; the deployment guard resolves this ID from its ownership ledger.')
param acaStorageId string
@description('Receipt-owned Flex Storage account; no storage accounts are created by this template.')
param flexStorageId string
@allowed(['/subscriptions/18827cdf-a4e8-4316-a7ba-a4499139a3ec'])
param inboundSubscriptionId string = '/subscriptions/18827cdf-a4e8-4316-a7ba-a4499139a3ec'

resource perimeter 'Microsoft.Network/networkSecurityPerimeters@2024-07-01' = {
  name: perimeterName
  location: location
  tags: {
    advisorDeploymentId: ownerRunId
    experiment: 'network'
    workload: 'copilot-advisor'
  }
  properties: {}
}

resource profile 'Microsoft.Network/networkSecurityPerimeters/profiles@2024-07-01' = {
  parent: perimeter
  name: 'storage'
  properties: {}
}

resource inbound 'Microsoft.Network/networkSecurityPerimeters/profiles/accessRules@2024-07-01' = {
  parent: profile
  name: 'owned-subscription'
  properties: {
    direction: 'Inbound'
    subscriptions: [
      { id: inboundSubscriptionId }
    ]
  }
}

resource acaAssociation 'Microsoft.Network/networkSecurityPerimeters/resourceAssociations@2024-07-01' = {
  parent: perimeter
  name: 'aca-state'
  properties: {
    accessMode: 'Enforced'
    privateLinkResource: { id: acaStorageId }
    profile: { id: profile.id }
  }
  dependsOn: [inbound]
}

resource flexAssociation 'Microsoft.Network/networkSecurityPerimeters/resourceAssociations@2024-07-01' = {
  parent: perimeter
  name: 'flex-state'
  properties: {
    accessMode: 'Enforced'
    privateLinkResource: { id: flexStorageId }
    profile: { id: profile.id }
  }
  dependsOn: [inbound]
}

output resourceIds array = [perimeter.id, profile.id, inbound.id, acaAssociation.id, flexAssociation.id]
output storageIds array = [acaStorageId, flexStorageId]
