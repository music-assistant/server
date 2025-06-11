"""Helper to provide the GraphQL Queries."""

from gql import gql

organizations_query = gql("""
query Nodes {
  organizations {
    nodes {
      coreId
      name
      title
      images {
        nodes {
          title
          imageId
          url
          width
        }
      }
    publicationServicesByOrganizationName {
      nodes {
          coreId
            }
        }
    }
  }
}
""")

publications_query = gql("""
query Query ($coreId: String!) {
  organizationByCoreId(coreId: $coreId) {
    publicationServicesByOrganizationName {
      nodes {
          coreId
          title
          synopsis
          imagesList {
            title
            url
            width
          }

      }
    }
  }
}
""")

radio_list_query = gql("""
query PermanentLivestreamByCoreId($coreId: String!) {
  publicationServiceByCoreId(coreId: $coreId) {
    title
    permanentLivestreams {
      nodes {
        current
        title
        audioList {
          audioBitrate
          href
          audioCodec
        }
        coreId
        summary
      }
    }
    genre
    synopsis
    imagesList {
      url
      width
      title
    }
    socialMediaAccounts {
      url
      service
    }
  }
}
""")


radio_metadata_query = gql("""
query PermanentLivestreamByCoreId($coreId: String!) {
  publicationServiceByCoreId(coreId: $coreId) {
    genre
    imagesList {
      url
      width
      title
    }
    socialMediaAccounts {
      url
      service
    }
    homepageUrl
    synopsis
  }
}
""")

livestream_query = gql("""
query PermanentLivestreamByCoreId($coreId: String!) {
  permanentLivestreamByCoreId(coreId: $coreId) {
    publisherCoreId
    summary
    current
    title
    imagesList {
      title
      url
      width
    }
    audioList {
        audioBitrate
        href
        audioCodec
    }
  }
}
""")

query = gql("""
query Nodes {
  organizations {
    nodes {
      name
      title
      publicationServicesByOrganizationName {
        nodes {
          title
          genre
          permanentLivestreams {
            nodes {
              audioList {
                href
                audioCodec
                distributionType
                audioBitrate
                audioChannel
              }
              description
            }
          }
          description
          socialMediaAccounts {
            service
            url
          }
          homepageUrl
          synopsis
          image {
            url
            description
            attribution
            url1X1
          }
          nodeId
        }
      }
      _links
      images {
        nodes {
          title
          imageId
          url
        }
      }
    }
  }
}
""")
